"""
Core ask pipeline — shared logic untuk /ask dan /ask/stream.

run_ask_pipeline() mengekstrak seluruh routing dan business logic dari
ask_question() di main.py. Kedua endpoint memanggil fungsi ini.

Kembalian: dict dengan semua field AskResponse + key internal:
  _bg_tasks  : list[(func, args, kwargs)] — critical (save_chat_history)
  _bg_queue  : list[(func, args, kwargs)] — non-critical (set_cache, log_rag)
  _bg_trigger: dict | None               — payload untuk trigger_background_jobs
"""

import re
import logging
import asyncio
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Callable, Awaitable

from fastapi import HTTPException

from app.services.core.jenjang_constants import JENJANG_MAP
from app.services.core.cognitive_router import classify_intent, QueryRoute, is_education_query
from app.services.core.intent_classifier import (
    classify_intent_rule as rule_classify,
    IntentType as RuleIntentType,
    is_clearly_oot,
)
from app.services.core.multi_intent_orchestrator import orchestrate
from app.services.core.school_name_normalizer import normalize_school_name  # noqa: F401
from app.services.core.result_model import ResultStatus, build_ambiguous_message
from app.services.query_handlers.aggregation_handler import (
    handle_aggregation_query,
    detect_ranking_level,
    is_ranking_query,
)
from app.services.query_handlers.school_vs_handler import (
    is_school_vs_school_query,
    handle_school_vs_school,
    format_school_comparison_markdown,
    narrate_school_comparison,
)
from app.services.query_handlers.comparison_handler import handle_comparison_query
from app.services.narrator_service import (
    narrate_out_of_context,
    narrate_aggregation,
    narrate_comparison_lokasi,
    narrate_comparison_status,
    narrate_school_list,
    narrate_school_profile,
    detect_level,
    narrate_multi_module,
    narrate_qc_percentage,
)
from app.services.formatters.response_formatter import (
    format_school_answer,
    format_school_list_answer,
    build_reject_answer,
    format_clarification_answer,
    build_location_label,
)
from app.services.search.search_service import (
    perform_search,
    compute_context_sufficiency_score,
    perform_geo_search,
    detect_geo_query,
    geocode_place_name,
    extract_place_from_query,
)
from app.services.confidence_engine import compute_rag_confidence, compute_aggregation_confidence
from app.router import route_with_fallback
from app.utils.cache import get_cache, set_cache, get_chat_history, save_chat_history
from app.utils.markdown_to_html import markdown_to_safe_html
from app.utils.rag_evaluation_logger import log_rag_evaluation

logger = logging.getLogger(__name__)

_OnStep = Optional[Callable[[str, str], Awaitable[None]]]


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _ms(start: datetime) -> int:
    return int((datetime.now() - start).total_seconds() * 1000)


def build_cache_key(query: str, level: Optional[str]) -> str:
    raw = f"{query}:{level}"
    return f"response:{hashlib.md5(raw.encode()).hexdigest()}"


def _extract_radius_from_query(query: str) -> Optional[float]:
    q = query.lower()
    for pat in [
        r'radius\s+(\d+(?:\.\d+)?)\s*km',
        r'dalam\s+radius\s+(\d+(?:\.\d+)?)\s*km',
        r'(\d+(?:\.\d+)?)\s*km\b',
        r'(\d+(?:\.\d+)?)\s*kilometer',
    ]:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1))
            if 0.1 <= val <= 50:
                return val
    for pat in [
        r'radius\s+(\d+(?:\.\d+)?)\s*(?:m\b|meter)',
        r'dalam\s+(\d+(?:\.\d+)?)\s*(?:m\b|meter)',
        r'(\d+(?:\.\d+)?)\s*meter',
    ]:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1)) / 1000
            if 0.05 <= val <= 5:
                return round(val, 2)
    return None


def _extract_ambiguous_message(result: dict) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    is_ambiguous = (
        status == ResultStatus.AMBIGUOUS
        or status == "AMBIGUOUS"
        or str(status) == "ResultStatus.AMBIGUOUS"
    )
    if not is_ambiguous:
        return None
    data = result.get("data", {})
    if data.get("message"):
        return data["message"]
    return build_ambiguous_message(data.get("candidates", []))


def _base(
    query: str,
    start: datetime,
    user_id: str,
    answer_markdown: str,
    provider: str,
    *,
    context_used: List[str] = None,
    context_sufficiency_score: float = 0.0,
    answer_confident: bool = False,
    confidence_score: Optional[float] = None,
    completeness_score: Optional[float] = None,
    num_docs: int = 0,
    from_cache: bool = False,
    suggestions: Optional[List[str]] = None,
) -> dict:
    md = answer_markdown or ""
    return {
        "query": query,
        "answer_markdown": md,
        "answer_html": markdown_to_safe_html(md),
        "provider": provider,
        "from_cache": from_cache,
        "duration_ms": _ms(start),
        "context_used": context_used or [],
        "context_sufficiency_score": context_sufficiency_score,
        "answer_confident": answer_confident,
        "confidence_score": confidence_score,
        "completeness_score": completeness_score,
        "user_id": user_id,
        "num_docs": num_docs,
        "suggestions": suggestions,
        "_bg_tasks": [],
        "_bg_queue": [],
        "_bg_trigger": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE SUB-FUNCTIONS (Phase 14)
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_geo_params(
    query: str,
    lat: Optional[float],
    lon: Optional[float],
    radius_km: float,
) -> tuple:
    """
    Ekstrak koordinat dari query jika tidak diberikan eksplisit.
    Diekstrak dari run_ask_pipeline() (Phase 14).
    """
    _lat, _lon = lat, lon
    _radius_km = radius_km

    if _lat is None or _lon is None:
        coord_match = re.search(
            r'(-?\d{1,3}(?:\.\d+)?)\s*[/,\s]\s*(-?\d{1,3}(?:\.\d+)?)',
            query,
        )
        if coord_match:
            try:
                lat_c = float(coord_match.group(1))
                lon_c = float(coord_match.group(2))
                if -90 <= lat_c <= 90 and -180 <= lon_c <= 180:
                    _lat, _lon = lat_c, lon_c
            except (ValueError, TypeError):
                pass

    extracted_radius = _extract_radius_from_query(query)
    if extracted_radius and radius_km == 2.0:
        _radius_km = extracted_radius

    return _lat, _lon, _radius_km


async def _do_step(on_step: _OnStep, name: str, msg: str) -> None:
    if on_step:
        try:
            await on_step(name, msg)
        except Exception:
            pass


async def _run_geo_fastpath(
    query: str,
    es_client,
    lat: Optional[float],
    lon: Optional[float],
    radius_km: float,
    start_time: datetime,
    user_id: str,
    on_step: _OnStep,
) -> dict:
    """
    GEO SEARCH FAST PATH — jika ada koordinat atau geo keyword.
    Geocode tempat jika belum ada koordinat, lalu query ES geo.
    Diekstrak dari run_ask_pipeline() (Phase 14).
    """
    # Geocode jika koordinat masih kosong
    if (lat is None or lon is None) and detect_geo_query(query):
        _place_name = extract_place_from_query(query)
        if _place_name:
            _geocoded = await geocode_place_name(_place_name)
            if _geocoded:
                lat, lon = _geocoded

    if lat is not None and lon is not None:
        await _do_step(on_step, "searching", "Mencari sekolah terdekat...")

        _q_lower_geo = query.lower()
        _geo_bentuk = next(
            (v for k, v in JENJANG_MAP.items()
             if re.search(r'\b' + k + r'\b', _q_lower_geo)),
            None,
        )
        _has_negeri = bool(re.search(r'\bnegeri\b', _q_lower_geo))
        _has_swasta = bool(re.search(r'\bswasta\b', _q_lower_geo))
        _geo_status = None
        if _has_negeri and not _has_swasta:
            _geo_status = "NEGERI"
        elif _has_swasta and not _has_negeri:
            _geo_status = "SWASTA"

        _akred_match  = re.search(r'\bakreditasi\s+([abc])\b', _q_lower_geo, re.I)
        _akred_match2 = re.search(r'\bterakreditasi\s+([abc])\b', _q_lower_geo, re.I)
        _geo_akred = None
        if _akred_match:
            _geo_akred = _akred_match.group(1).upper()
        elif _akred_match2:
            _geo_akred = _akred_match2.group(1).upper()

        _geo_result = await perform_geo_search(
            es_client=es_client,
            lat=lat, lon=lon,
            radius_km=radius_km,
            bentuk=_geo_bentuk,
            status=_geo_status,
            akreditasi=_geo_akred,
            size=10,
        )
        _geo_hits = _geo_result.get("hits", []) if isinstance(_geo_result, dict) else (_geo_result or [])
        _total_in_radius = (
            _geo_result.get("total_in_radius", len(_geo_hits))
            if isinstance(_geo_result, dict) else len(_geo_hits)
        )
        _breakdown_status  = _geo_result.get("breakdown_status", {})
        _breakdown_jenjang = _geo_result.get("breakdown_jenjang", {})

        if not _geo_hits:
            _empty_msg = (
                f"Tidak ditemukan sekolah dalam radius **{radius_km} km** "
                f"dari koordinat yang diberikan"
            )
            if _geo_bentuk:
                _empty_msg += f" dengan jenjang **{_geo_bentuk}**"
            if _geo_status:
                _empty_msg += f" berstatus **{_geo_status}**"
            _empty_msg += ". Coba perluas radius pencarian."
            return _base(query, start_time, user_id, _empty_msg,
                         "ELASTICSEARCH_GEO", context_used=[f"geo:{lat},{lon}"])

        _schools = []
        for h in _geo_hits:
            s = h["_source"]
            _qc            = s.get("qc_score") or {}
            _total_masalah = int(_qc.get("total_masalah") or 0)
            _masalah_keys  = (
                [k for k, v in _qc.items()
                 if k.startswith("qc_") and k != "total_masalah" and v]
                if _total_masalah > 0 else []
            )
            _schools.append({
                "nama": s.get("nama"), "npsn": s.get("npsn"),
                "bentuk": s.get("bentuk_pendidikan"), "status": s.get("status_sekolah"),
                "akreditasi": s.get("akreditasi"), "desa_kelurahan": s.get("desa_kelurahan"),
                "kecamatan": s.get("kecamatan"), "kabupaten": s.get("kabupaten"),
                "provinsi": s.get("provinsi"), "jarak_km": h.get("jarak_km"),
                "total_masalah": _total_masalah, "masalah_keys": _masalah_keys,
            })

        _info_total = f"Menampilkan **{len(_schools)}** dari **{_total_in_radius}** sekolah dalam radius {radius_km} km."
        _lines = [f"### Sekolah Terdekat (radius {radius_km} km)\n", _info_total, ""]
        for i, sc in enumerate(_schools, 1):
            _jarak  = f" - **{sc['jarak_km']} km**" if sc.get("jarak_km") else ""
            _akred  = f" | Akreditasi {sc['akreditasi']}" if sc.get("akreditasi") else ""
            _lokasi = ", ".join(filter(None, [sc.get("kecamatan"), sc.get("kabupaten")]))
            _lines.append(
                f"{i}. **{sc['nama']}** ({sc.get('bentuk','')} {sc.get('status','')})"
                f"{_akred} - {_lokasi}{_jarak}"
            )
        _template_md = "\n".join(_lines)

        _STATUS_NOISE = {"aktif", "aktif (buka)", "buka"}
        _bd_status  = {k: v for k, v in _breakdown_status.items() if k.lower().strip() not in _STATUS_NOISE}
        _negeri_total = _bd_status.get("NEGERI", 0) + _bd_status.get("Negeri", 0)
        _swasta_total = _bd_status.get("SWASTA", 0) + _bd_status.get("Swasta", 0)
        _total_ns     = _negeri_total + _swasta_total
        _user_asks_status = bool(re.search(r"\b(negeri|swasta|status)\b", query, re.I))
        _status_note = ""
        if _total_ns > 0:
            _pct_negeri = round(_negeri_total / _total_ns * 100)
            if _negeri_total == 0:
                _status_note = (
                    f"⚠️ Dalam radius {radius_km} km ini "
                    f"**tidak ditemukan sekolah berstatus NEGERI**. "
                    f"Seluruh {_total_in_radius:,} sekolah yang terdeteksi berstatus swasta."
                )
            elif _user_asks_status or _swasta_total > _negeri_total * 2:
                _status_note = (
                    f"ℹ️ Dari **{_total_in_radius:,} sekolah** dalam radius ini: "
                    f"**{_negeri_total:,} Negeri** ({_pct_negeri}%) dan "
                    f"**{_swasta_total:,} Swasta** ({100 - _pct_negeri}%). "
                    + ("Area ini didominasi sekolah swasta." if _swasta_total > _negeri_total else "")
                )
        if _status_note:
            _template_md = f"{_status_note}\n\n{_template_md}"

        _geo_active_filters = {}
        if _geo_bentuk: _geo_active_filters["bentuk"]     = _geo_bentuk
        if _geo_status: _geo_active_filters["status"]     = _geo_status
        if _geo_akred:  _geo_active_filters["akreditasi"] = _geo_akred

        await _do_step(on_step, "narrating", "Menyusun jawaban...")
        _md = await narrate_school_list(
            wilayah=f"radius {radius_km} km dari koordinat {lat:.4f},{lon:.4f}",
            total=_total_in_radius, schools=_schools, template_md=_template_md,
            breakdown_jenjang=_breakdown_jenjang, breakdown_status=_bd_status,
            active_filters=_geo_active_filters,
        )

        _geo_ctx = [f"geo:{lat},{lon}"]
        if _geo_bentuk: _geo_ctx.append(f"bentuk:{_geo_bentuk}")
        if _geo_akred:  _geo_ctx.append(f"akreditasi:{_geo_akred}")
        if _geo_status: _geo_ctx.append(f"status:{_geo_status}")

        return _base(query, start_time, user_id, _md, "ELASTICSEARCH_GEO",
                     context_used=_geo_ctx, context_sufficiency_score=0.9,
                     answer_confident=True, confidence_score=0.9, num_docs=len(_schools))

    _geo_msg = (
        "Untuk mencari sekolah terdekat, saya membutuhkan koordinat lokasi Anda.\n\n"
        "Silakan kirimkan parameter `lat` dan `lon` bersama pertanyaan."
    )
    return _base(query, start_time, user_id, _geo_msg, "SYSTEM_GEO_NO_COORDS")


async def _run_classification(query: str, on_step: _OnStep) -> tuple:
    """
    Classify intent via Tier1+2+3 + adjudication.
    Return (CognitiveIntent, from_cache: bool).
    Diekstrak dari run_ask_pipeline() (Phase 14).
    """
    await _do_step(on_step, "classifying", "Menganalisis pertanyaan...")
    intent = await classify_intent(query, timeout=45.0, use_cache=True)
    logger.info("[RouterV2] primary=%s secondary=%s", intent.primary, intent.secondary)

    # Adjudication: rule-based classifier sebagai tiebreaker
    if intent.primary == "out_of_context" and not intent.secondary:
        rule_result = rule_classify(query)
        if rule_result == RuleIntentType.SCHOOL:
            intent.primary = "list"
            intent.is_oot = False
            intent.oot_reason = None
        elif rule_result == RuleIntentType.AMBIGUOUS:
            _q = query.lower()
            _edu_kws = (
                "sekolah", "sd", "smp", "sma", "smk", "tk", "paud",
                "mi", "mts", "ma", "madrasah", "pesantren", "pondok",
                "pkbm", "slb", "skb", "guru", "siswa", "murid",
                "tendik", "akreditasi", "npsn",
            )
            if any(kw in _q for kw in _edu_kws):
                intent.primary = "list"
                intent.is_oot = False
                intent.oot_reason = None

    # Ranking override: LLM sometimes classifies ranking queries as "list".
    # Detect early via keyword signal and redirect to aggregation handler.
    if intent.primary == "list" and is_ranking_query(query) and detect_ranking_level(query):
        logger.info("[RouterV2] ranking override: list → aggregation for '%s'", query[:60])
        intent.primary = "aggregation"

    return intent, False


async def _handle_comparison_route(
    query: str,
    intent,
    es_client,
    profile_handler,
    start_time: datetime,
    user_id: str,
    on_step: _OnStep,
) -> Optional[dict]:
    """
    Handle COMPARISON route — school-vs-school + lokasi comparison.
    Return dict jika berhasil, None jika harus fall-through ke LIST.
    Diekstrak dari run_ask_pipeline() (Phase 14).
    """
    if is_school_vs_school_query(query):
        await _do_step(on_step, "searching", "Mencari profil dua sekolah...")
        vs_result = await handle_school_vs_school(query, profile_handler)
        if vs_result and vs_result.get("status") == "success":
            await _do_step(on_step, "narrating", "Menyusun perbandingan sekolah...")
            cmp_md = format_school_comparison_markdown(vs_result)
            cmp_md = await narrate_school_comparison(vs_result, cmp_md)
            return _base(query, start_time, user_id, cmp_md, "ELASTICSEARCH_SCHOOL_VS",
                         context_used=[
                             f"npsn:{vs_result['school_a'].get('data', {}).get('identitas', {}).get('npsn', '')}",
                             f"npsn:{vs_result['school_b'].get('data', {}).get('identitas', {}).get('npsn', '')}",
                         ],
                         context_sufficiency_score=1.0, answer_confident=True,
                         confidence_score=0.90, num_docs=2)

    await _do_step(on_step, "searching", "Membandingkan data wilayah...")
    try:
        comparison_result = await handle_comparison_query(
            query=query, es_client=es_client, intent=intent)
        _amb = _extract_ambiguous_message(comparison_result)
        if _amb:
            return _base(query, start_time, user_id, _amb, "SYSTEM_AMBIGUOUS",
                         answer_confident=False, confidence_score=0.5)
    except Exception:
        logger.exception("[COMPARISON] handler crashed")
        raise HTTPException(500, "Comparison internal error")

    if not comparison_result or not isinstance(comparison_result, dict):
        logger.info("[COMPARISON] no result, fallback → LIST")
        return None

    prov = comparison_result.get("provider", "")

    if prov == "SYSTEM_ERROR":
        return _base(query, start_time, user_id,
                     "Terjadi kesalahan dalam memproses perbandingan.", "SYSTEM_ERROR")

    if prov == "SYSTEM_VALIDATION":
        reason = comparison_result.get("reason", "")
        if reason == "COMPARISON_NEEDS_MULTIPLE_LOCATIONS":
            md = "Untuk perbandingan, sebutkan minimal dua lokasi.\n\nContoh: _\"bandingkan cisauk vs parungpanjang\"_"
        elif reason == "HIERARCHY_INCOMPLETE":
            md = "Lokasi tidak lengkap atau tidak dikenali."
        else:
            md = "Lokasi tidak dikenali. Coba perjelas nama wilayah."
        return _base(query, start_time, user_id, md, "SYSTEM_VALIDATION")

    if prov == "ELASTICSEARCH_COMPARISON_STATUS":
        answer_md   = comparison_result.get("answer_markdown", "")
        total_hits  = comparison_result.get("num_docs", 0)
        conf        = comparison_result.get("confidence", 0.0)
        results_raw = comparison_result.get("results", [])
        counts  = {s.lower(): c for s, c in results_raw}
        negeri  = counts.get("negeri", 0)
        swasta  = counts.get("swasta", 0)
        pct_diff = 0
        if min(negeri, swasta) > 0:
            dom     = max(negeri, swasta)
            non_dom = min(negeri, swasta)
            pct_diff = round((dom - non_dom) / non_dom * 100)
        wm = re.search(r"\*\*Wilayah:\s*(.+?)\*\*", answer_md)
        wlabel = wm.group(1) if wm else "wilayah ini"
        await _do_step(on_step, "narrating", "Menyusun narasi perbandingan...")
        answer_md = await narrate_comparison_status(
            wilayah=wlabel, negeri=negeri, swasta=swasta,
            pct_diff=pct_diff, template_md=answer_md)
        return _base(query, start_time, user_id, answer_md,
                     "ELASTICSEARCH_COMPARISON_STATUS",
                     context_sufficiency_score=1.0,
                     answer_confident=comparison_result.get("answer_confident", False),
                     confidence_score=conf, num_docs=total_hits)

    if prov == "ELASTICSEARCH_COMPARISON_JENJANG":
        return _base(query, start_time, user_id,
                     comparison_result.get("answer_markdown", ""),
                     "ELASTICSEARCH_COMPARISON_JENJANG",
                     context_sufficiency_score=1.0,
                     answer_confident=comparison_result.get("answer_confident", False),
                     confidence_score=comparison_result.get("confidence", 0.0),
                     num_docs=comparison_result.get("num_docs", 0))

    if prov == "ELASTICSEARCH_COMPARISON":
        results        = comparison_result.get("results", [])
        results_detail = comparison_result.get("results_detail", [])
        answer_md      = comparison_result.get("answer_markdown", "")
        total_sum      = sum(c for _, c in results)
        conf           = compute_aggregation_confidence(
            query=query, resolution_status="SUCCESS",
            resolution_metadata={}, total_hits=total_sum)

        if not results:
            return _base(query, start_time, user_id,
                         "Data tidak ditemukan untuk perbandingan.", "SYSTEM_NO_DATA",
                         context_sufficiency_score=1.0, confidence_score=conf)

        if not answer_md:
            lines = [f"- {loc}: **{count:,} sekolah**" for loc, count in results]
            answer_md = "### Perbandingan Jumlah Sekolah\n\n" + "\n".join(lines)

        narrator_results = []
        for i, (loc, count) in enumerate(results):
            item = {"lokasi": loc, "count": count}
            if i < len(results_detail):
                rd = results_detail[i]
                item["breakdown_jenjang"] = rd.get("breakdown_jenjang", {})
                item["breakdown_status"]  = rd.get("breakdown_status", {})
                if rd.get("avg_siswa"):   item["avg_siswa"]   = round(rd["avg_siswa"])
                if rd.get("total_siswa"): item["total_siswa"] = rd["total_siswa"]
                if rd.get("max_siswa"):   item["max_siswa"]   = rd["max_siswa"]
            narrator_results.append(item)

        await _do_step(on_step, "narrating", "Menyusun narasi perbandingan...")
        answer_md = await narrate_comparison_lokasi(
            results=narrator_results, template_md=answer_md,
            results_detail=narrator_results, query=query)

        ctx_list = []
        for r in (results_detail or []):
            ent = r.get("entities", {})
            ctx_list.extend(f"{k}:{v}" for k, v in ent.items() if v)

        return _base(query, start_time, user_id, answer_md, "ELASTICSEARCH_COMPARISON",
                     context_used=ctx_list, context_sufficiency_score=1.0,
                     answer_confident=conf >= 0.6, confidence_score=conf, num_docs=total_sum)

    logger.info("[COMPARISON] unhandled provider=%s, fallback → LIST", prov)
    return None


async def _handle_aggregation_route(
    query: str,
    intent,
    es_client,
    user_id: str,
    start_time: datetime,
    on_step: _OnStep,
) -> Optional[dict]:
    """
    Handle AGGREGATION route.
    Return dict jika berhasil, None jika harus fall-through ke LIST.
    Diekstrak dari run_ask_pipeline() (Phase 14).
    """
    await _do_step(on_step, "searching", "Menghitung agregasi data sekolah...")
    try:
        result = await handle_aggregation_query(
            query=query, es_client=es_client, user_id=user_id, intent=intent)
        _amb = _extract_ambiguous_message(result)
        if _amb:
            return _base(query, start_time, user_id, _amb, "SYSTEM_AMBIGUOUS",
                         answer_confident=False, confidence_score=0.5)
    except Exception:
        logger.exception("[AGGREGATION] handler crashed")
        raise HTTPException(500, "Aggregation internal error")

    if not result:
        return None  # fall-through ke LIST

    agg_prov = result.get("provider", "")

    if agg_prov in ("SYSTEM_VALIDATION", "SYSTEM_ERROR"):
        if result.get("reason") == "HIERARCHY_INCOMPLETE":
            missing = result.get("missing_parent")
            lv      = result.get("level")
            answer_md = (
                f"Untuk menentukan **{lv}**, perlu sebutkan **{missing}** terlebih dahulu.\n\n"
                f"Contoh:\n- Jumlah sekolah di {lv} X di {missing} Y"
            )
        else:
            answer_md = "**Data tidak ditemukan atau lokasi tidak dikenali.**"
        conf = compute_aggregation_confidence(
            query=query, resolution_status="FAILED",
            resolution_metadata=result, total_hits=0)
        return _base(query, start_time, user_id, answer_md, agg_prov, confidence_score=conf)

    if agg_prov == "ELASTICSEARCH_GLOBAL_RANKING":
        ranking = result.get("ranking", [])
        lv      = detect_ranking_level(query)
        label_map = {
            "provinsi": "Provinsi", "kabupaten": "Kabupaten/Kota",
            "kecamatan": "Kecamatan", "desa_kelurahan": "Desa/Kelurahan",
        }
        label         = label_map.get(lv, "Wilayah")
        jenjang_label = result.get("jenjang") or "sekolah"
        conf          = result.get("confidence", 0.85)
        top_loc       = result.get("top_location", "")
        total         = result.get("total", 0)

        lines = [
            f"{i}. **{b.get('key', '?').title()}** — {b.get('doc_count', 0):,} {jenjang_label}"
            for i, b in enumerate(ranking, 1)
        ]
        answer_md = (
            f"### Top {len(ranking)} {label} dengan {jenjang_label.upper()} Terbanyak\n\n"
            + "\n".join(lines)
        )
        return _base(query, start_time, user_id, answer_md,
                     "ELASTICSEARCH_GLOBAL_RANKING",
                     context_used=[f"ranking:{lv}:{top_loc}"],
                     context_sufficiency_score=1.0,
                     answer_confident=conf >= 0.6, confidence_score=conf, num_docs=total)

    if agg_prov == "ELASTICSEARCH_QC_PERCENTAGE":
        data = result.get("data", {})
        answer_md = narrate_qc_percentage(
            total_sekolah=data.get("total_sekolah", 0),
            total_bersih=data.get("total_bersih", 0),
            total_bermasalah=data.get("total_bermasalah", 0),
            persen_bersih=data.get("persen_bersih", 0.0),
            persen_bermasalah=data.get("persen_bermasalah", 0.0),
            wilayah_label=data.get("wilayah_label", "seluruh Indonesia"),
        )
        return _base(query, start_time, user_id, answer_md, "ELASTICSEARCH_QC_PERCENTAGE",
                     context_sufficiency_score=1.0, answer_confident=True,
                     confidence_score=result.get("confidence", 0.95))

    await _do_step(on_step, "narrating", "Menyusun narasi agregasi...")

    if result.get("is_multi"):
        comparison_list = result.get("comparison", [])
        if not comparison_list:
            answer_md = "Data tidak ditemukan untuk lokasi yang dimaksud."
        else:
            comparison_list.sort(key=lambda x: x.get("total", 0), reverse=True)
            lines, narrator_results_multi = [], []
            for item in comparison_list:
                ent   = item.get("entities", {})
                total = item.get("total", 0)
                label = "Wilayah"
                for lvl in ["desa_kelurahan", "kecamatan", "kabupaten", "provinsi"]:
                    val = ent.get(lvl)
                    if val:
                        label = val.replace("kec.", "Kec.").replace("kab.", "Kab.").replace("prov.", "Prov.").title()
                        break
                lines.append(f"- {label}: **{total:,} sekolah**")
                narrator_results_multi.append({"lokasi": label, "count": total})
            answer_md = "### Perbandingan Jumlah Sekolah\n\n" + "\n".join(lines)
            if len(comparison_list) >= 2:
                best_ent   = comparison_list[0].get("entities", {})
                best_label = "Wilayah"
                for lvl in ["desa_kelurahan", "kecamatan", "kabupaten", "provinsi"]:
                    val = best_ent.get(lvl)
                    if val:
                        best_label = val.title()
                        break
                answer_md += f"\n\n---\nWilayah dengan sekolah terbanyak adalah **{best_label}**."
            answer_md = await narrate_comparison_lokasi(
                results=narrator_results_multi, template_md=answer_md, query=query)
    else:
        answer_md           = format_school_answer(result)
        entities_for_narasi = result.get("entities", {}) or {}
        wilayah_narasi      = build_location_label(entities_for_narasi)
        total_narasi        = result.get("total", 0) or 0
        status_narasi       = entities_for_narasi.get("status_sekolah")
        answer_md = await narrate_aggregation(
            wilayah=wilayah_narasi, total=total_narasi, template_md=answer_md,
            status_filter=status_narasi, level=detect_level(result.get("entities", {})),
            breakdown_jenjang=result.get("breakdown_jenjang"),
            breakdown_status=result.get("breakdown_status"),
            drilldown_top3=result.get("drilldown_top3"),
            drilldown_field=result.get("drilldown_field"),
            total_all=result.get("total_all"), pct_of_total=result.get("pct_of_total"),
            active_filters=result.get("active_filters"),
            jenjang_requested=result.get("jenjang_requested"),
            metric_target=result.get("metric_target"),
        )

    resolution_metadata = result.get("metadata", {}) or {}
    if resolution_metadata.get("low_confidence_desa"):
        entities = result.get("entities", {})
        desa_val = entities.get("desa_kelurahan", "")
        kab_val  = entities.get("kabupaten", "")
        answer_md += (
            f"\n\n> **Catatan:** Sistem menemukan satu data untuk "
            f"'{desa_val.title()}' di {kab_val.title()}. "
            f"Jika bukan lokasi yang dimaksud, tambahkan nama kabupaten atau kecamatan."
        )

    conf    = result.get("confidence", 0.0)
    entities = result.get("entities", {}) or {}
    if result.get("is_multi"):
        cmp_list      = result.get("comparison", [])
        total_docs    = sum(x.get("total", 0) for x in cmp_list)
        ctx_list      = [f"{k}:{v}" for item in cmp_list for k, v in item.get("entities", {}).items()]
    else:
        total_docs = result.get("total", 0)
        ctx_list   = [f"{k}:{v}" for k, v in entities.items()]

    return _base(query, start_time, user_id, answer_md, result.get("provider", "UNKNOWN"),
                 context_used=ctx_list, context_sufficiency_score=float(conf),
                 answer_confident=result.get("answer_confident", False),
                 confidence_score=conf, num_docs=total_docs)


async def _handle_profile_route(
    query: str,
    intent,
    profile_handler,
    start_time: datetime,
    user_id: str,
    on_step: _OnStep,
) -> dict:
    """
    Handle PROFILE route.
    Diekstrak dari run_ask_pipeline() (Phase 14).
    """
    if is_school_vs_school_query(query):
        await _do_step(on_step, "searching", "Mencari profil dua sekolah...")
        vs_result = await handle_school_vs_school(query, profile_handler)
        if vs_result and vs_result.get("status") == "success":
            await _do_step(on_step, "narrating", "Menyusun perbandingan sekolah...")
            cmp_md = format_school_comparison_markdown(vs_result)
            cmp_md = await narrate_school_comparison(vs_result, cmp_md)
            return _base(query, start_time, user_id, cmp_md, "ELASTICSEARCH_SCHOOL_VS",
                         context_used=[
                             f"npsn:{vs_result['school_a'].get('data', {}).get('identitas', {}).get('npsn', '')}",
                             f"npsn:{vs_result['school_b'].get('data', {}).get('identitas', {}).get('npsn', '')}",
                         ],
                         context_sufficiency_score=1.0, answer_confident=True,
                         confidence_score=0.90, num_docs=2)

    profile_cache_key = f"profile:v2:{hashlib.md5(query.encode()).hexdigest()}"
    await _do_step(on_step, "searching", "Mencari profil sekolah...")
    cached_profile = await get_cache(profile_cache_key)

    if cached_profile:
        if "answer_markdown" not in cached_profile:
            logger.warning("Invalid profile cache format, skipping")
        else:
            cached_profile["answer_html"] = markdown_to_safe_html(cached_profile["answer_markdown"])
            cached_profile["from_cache"]  = True
            cached_profile["duration_ms"] = 0
            return {**cached_profile, "_bg_tasks": [], "_bg_queue": [], "_bg_trigger": None}

    profile_result = await profile_handler.handle(query, intent=intent)
    logger.debug("[PROFILE] result status: %s", profile_result.get("status"))

    if profile_result["status"] == "not_found":
        return _base(query, start_time, user_id, "Sekolah tidak ditemukan.",
                     "ELASTICSEARCH_PROFILE", context_sufficiency_score=1.0)

    if profile_result["status"] == "ambiguous":
        candidates = profile_result["candidates"]
        msg        = profile_result.get("message", "Mohon perjelas sekolah yang dimaksud:")
        options_md = "\n".join(
            f"- {c.get('nama')} \n  Lokasi: {c.get('lokasi')} \n  NPSN: {c.get('npsn')}"
            for c in candidates
        )
        answer_md = "### Ditemukan Beberapa Sekolah\n\n" + msg + "\n\n" + options_md
        return _base(query, start_time, user_id, answer_md, "ELASTICSEARCH_PROFILE_AMBIGUOUS",
                     context_sufficiency_score=1.0, num_docs=len(candidates))

    profile_data = profile_result.get("data") or {}
    if not isinstance(profile_data, dict) or not profile_data:
        raise HTTPException(status_code=500, detail="Invalid profile format")

    formatted_md = profile_result.get("formatted")
    if not formatted_md:
        from app.services.formatters.profile_formatter import format_profile_markdown
        formatted_md = format_profile_markdown(profile_data)

    await _do_step(on_step, "narrating", "Menyusun profil sekolah...")
    formatted_md = await narrate_school_profile(
        profile_data=profile_data, template_md=formatted_md,
        query=query, is_weak_match=profile_result.get("weak_match", False))

    npsn_value         = profile_data.get("identitas", {}).get("npsn")
    completeness_score = profile_result.get("completeness_score", 0.0)
    conf               = profile_result.get("confidence", 0.0)

    _tahun_data = profile_data.get("tahun") or profile_data.get("identitas", {}).get("tahun")
    try:
        _tahun_int = int(_tahun_data) if _tahun_data else 0
    except (ValueError, TypeError):
        _tahun_int = 0
    _profile_ttl = 86400 if (_tahun_int > 0 and (datetime.now().year - _tahun_int) >= 2) else 3600

    resp = _base(query, start_time, user_id, formatted_md, "ELASTICSEARCH_PROFILE",
                 context_used=[f"npsn:{npsn_value}"] if npsn_value else [],
                 context_sufficiency_score=1.0,
                 answer_confident=conf >= 0.6, confidence_score=conf,
                 completeness_score=completeness_score, num_docs=1)

    cache_payload = {k: v for k, v in resp.items() if not k.startswith("_")}
    resp["_bg_queue"].append((set_cache, [profile_cache_key, cache_payload, _profile_ttl], {}))
    return resp


async def _handle_list_route(
    query: str,
    intent,
    list_handler,
    start_time: datetime,
    user_id: str,
    on_step: _OnStep,
) -> Optional[dict]:
    """
    Handle LIST route.
    Return dict jika berhasil, None jika tidak ada data.
    Diekstrak dari run_ask_pipeline() (Phase 14).
    """
    await _do_step(on_step, "searching", "Mencari daftar sekolah...")
    result = await list_handler.handle(query, intent=intent)
    _amb = _extract_ambiguous_message(result)
    if _amb:
        return _base(query, start_time, user_id, _amb, "SYSTEM_AMBIGUOUS",
                     answer_confident=False, confidence_score=0.5)

    status_res = result.get("status")
    schools    = result.get("schools", [])
    total      = result.get("total", 0)

    if status_res == "clarification":
        formatted = format_clarification_answer(result)
        return _base(query, start_time, user_id, formatted["answer_markdown"],
                     "ELASTICSEARCH_LIST", context_sufficiency_score=1.0)

    if not schools:
        return _base(query, start_time, user_id,
                     "Data sekolah tidak ditemukan untuk lokasi tersebut.",
                     "ELASTICSEARCH_LIST", context_sufficiency_score=1.0)

    formatted    = format_school_list_answer(result)
    wilayah_list = result.get("parsed", {}).get("wilayah", "") or "wilayah yang diminta"
    _parsed      = result.get("parsed", {})
    _active_filters = {
        k: v for k, v in {
            "bentuk":          _parsed.get("bentuk"),
            "status":          _parsed.get("status"),
            "akreditasi":      _parsed.get("akreditasi"),
            "qc_masalah_only": _parsed.get("qc_masalah_only"),
            "qc_field_list":   _parsed.get("qc_field_list"),
            "qc_field":        _parsed.get("qc_field"),
        }.items() if v
    }
    _dan_split = _parsed.get("dan_split_breakdown")
    if _dan_split:
        _active_filters["dan_split_breakdown"] = _dan_split
        _active_filters["dan_split_total"] = _parsed.get("dan_split_total", total)

    await _do_step(on_step, "narrating", "Menyusun daftar sekolah...")
    formatted["answer_markdown"] = await narrate_school_list(
        wilayah=wilayah_list, total=total, schools=schools,
        template_md=formatted["answer_markdown"],
        breakdown_jenjang=result.get("breakdown_jenjang"),
        breakdown_status=result.get("breakdown_status"),
        active_filters=_active_filters,
        wilayah_level=result.get("wilayah_level", ""),
    )

    conf = compute_aggregation_confidence(
        query=query, resolution_status="SUCCESS",
        resolution_metadata=result.get("metadata", {}), total_hits=total)
    _wlevel          = result.get("wilayah_level", "")
    is_desa_level    = _wlevel == "desa_kelurahan"
    answer_confident = conf >= (0.5 if is_desa_level else 0.6)
    _level_key = _wlevel if _wlevel in ("provinsi", "kabupaten", "kecamatan", "desa_kelurahan") else "wilayah"
    ctx_parts = [f"{_level_key}:{wilayah_list}"] if wilayah_list else []
    if _parsed.get("bentuk"):     ctx_parts.append(f"bentuk_pendidikan:{_parsed['bentuk']}")
    if _parsed.get("status"):     ctx_parts.append(f"status:{_parsed['status']}")
    if _parsed.get("akreditasi"): ctx_parts.append(f"akreditasi:{_parsed['akreditasi']}")

    return _base(query, start_time, user_id, formatted["answer_markdown"], "ELASTICSEARCH_LIST",
                 context_used=ctx_parts, context_sufficiency_score=float(conf),
                 answer_confident=answer_confident, confidence_score=conf, num_docs=total)


async def _handle_rag_fallback(
    query: str,
    intent,
    query_level: Optional[str],
    es_client,
    user_id: str,
    start_time: datetime,
    on_step: _OnStep,
) -> dict:
    """
    RAG FLOW — fallback terakhir jika semua route di atas tidak match.
    Diekstrak dari run_ask_pipeline() (Phase 14).
    """
    cache_key = build_cache_key(query, query_level)
    cached = await get_cache(cache_key)
    if cached:
        cached["answer_html"] = markdown_to_safe_html(cached["answer_markdown"])
        cached["from_cache"]  = True
        cached["duration_ms"] = 0
        return {**cached, "_bg_tasks": [], "_bg_queue": [], "_bg_trigger": None}

    await _do_step(on_step, "searching", "Mencari dokumen relevan...")
    history     = await get_chat_history(user_id) or []
    _user_turns = [m for m in history if m.get("role") == "user"][-3:]
    history_context = "\n".join(f"user: {m['content']}" for m in _user_turns)

    _loc_filter: Dict[str, str] = {}
    if intent:
        _w = getattr(intent, "wilayah", None)
        if _w:
            if getattr(_w, "provinsi", None):  _loc_filter["provinsi"]  = _w.provinsi
            if getattr(_w, "kabupaten", None): _loc_filter["kabupaten"] = _w.kabupaten
            if getattr(_w, "kecamatan", None): _loc_filter["kecamatan"] = _w.kecamatan
        if "kecamatan" in _loc_filter:
            _loc_filter = {"kecamatan": _loc_filter["kecamatan"]}
        elif "kabupaten" in _loc_filter:
            _loc_filter = {"kabupaten": _loc_filter["kabupaten"]}
        elif "provinsi" in _loc_filter:
            _loc_filter = {"provinsi": _loc_filter["provinsi"]}

    hits = await perform_search(
        es_client=es_client, query_text=query, size=5,
        filter_level=query_level, location_filter=_loc_filter or None)
    retrieval_score = compute_context_sufficiency_score(hits)

    if retrieval_score < 0.3 and _loc_filter:
        try:
            from app.background.retrieval_retry_handler import retry_with_expanded_query
            retry_hits, was_retried = await retry_with_expanded_query(
                es_client=es_client, query=query, original_loc_filter=_loc_filter,
                size=5, score_before=retrieval_score)
            if was_retried and retry_hits:
                hits = retry_hits
                retrieval_score = compute_context_sufficiency_score(hits)
        except Exception:
            logger.debug("[RETRY] Retrieval retry failed", exc_info=True)

    _CONTEXT_BUDGET = 4000
    _per_doc_limit  = _CONTEXT_BUDGET // len(hits) if hits else 0
    _EXTRA_SOURCE_FIELDS = (
        "siswa_l", "siswa_p", "siswa_total", "tendik_l", "tendik_p",
        "nilai_akhir", "kode_pos", "guru_total", "guru_pns", "guru_honor", "guru_sertifikasi",
    )

    def _build_doc_ctx(idx: int, hit: dict, char_limit: int) -> str:
        src  = hit.get("_source", {})
        text = src.get("content", "")[:char_limit]
        extras = {k: src[k] for k in _EXTRA_SOURCE_FIELDS if src.get(k) is not None}
        if extras:
            text = f"{text} [" + " | ".join(f"{k}={v}" for k, v in extras.items()) + "]"
        return f"[SUMBER {idx + 1}]: {text}"

    context = "\n\n".join(_build_doc_ctx(i, h, _per_doc_limit) for i, h in enumerate(hits)) or "Tidak ada data relevan."

    if retrieval_score < 0.3:
        return _base(query, start_time, user_id,
                     "Data tidak cukup relevan untuk menjawab pertanyaan.",
                     "SYSTEM_LOW_RETRIEVAL", num_docs=len(hits))

    await _do_step(on_step, "narrating", "Menyusun jawaban dengan AI...")
    ai_result  = await route_with_fallback(query=query, initial_context=context,
                                           history_context=history_context)
    evaluation = ai_result.get("evaluation") or {}
    llm_score  = evaluation.get("score", 0)

    if llm_score < 30 and retrieval_score < 0.4:
        return _base(query, start_time, user_id,
                     "Data yang tersedia belum cukup kuat untuk memberikan jawaban yang akurat. "
                     "Silakan perjelas pertanyaan atau lokasi yang dimaksud.",
                     "SYSTEM_LOW_CONTEXT",
                     context_sufficiency_score=round(float(llm_score) / 100, 3),
                     num_docs=len(hits))

    final_response   = ai_result["response"]
    conf             = compute_rag_confidence(
        llm_score=llm_score, retrieval_score=retrieval_score, num_docs=len(hits))
    answer_confident = conf >= 0.6

    _suggestions: Optional[List[str]] = None
    try:
        from app.background.suggestion import generate_suggestions, get_suggestions
        await asyncio.wait_for(
            generate_suggestions({"query": query}, max_suggestions=4), timeout=1.2)
        _sug = await get_suggestions(query, max_suggestions=4)
        _suggestions = _sug.get("suggestions") or None
    except Exception:
        pass

    resp = _base(query, start_time, user_id, final_response, ai_result["provider"],
                 context_sufficiency_score=round(float(llm_score) / 100, 3),
                 answer_confident=answer_confident, confidence_score=conf,
                 num_docs=len(hits), suggestions=_suggestions)

    cache_payload = {k: v for k, v in resp.items() if not k.startswith("_")}
    resp["_bg_queue"] = [
        (set_cache, [cache_key, cache_payload, 600], {}),
        (log_rag_evaluation, [], {
            "query": query, "duration_ms": resp["duration_ms"],
            "context_sufficiency_score": llm_score,
            "answer_confident": answer_confident,
            "num_docs": len(hits), "answer_length": len(final_response),
        }),
    ]
    resp["_bg_tasks"] = [
        (save_chat_history, [user_id, "user", query], {}),
        (save_chat_history, [user_id, "assistant", final_response], {}),
    ]
    resp["_bg_trigger"] = cache_payload
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

async def run_ask_pipeline(
    query: str,
    user_id: str,
    es_client,
    list_handler,
    profile_handler,
    level: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    radius_km: float = 2.0,
    start_time: Optional[datetime] = None,
    on_step: _OnStep = None,
) -> dict:
    """
    Core pipeline untuk /ask dan /ask/stream.

    on_step: async callback dipanggil di setiap step utama.
             Signature: on_step(step_name: str, message: str)
    """
    if start_time is None:
        start_time = datetime.now()

    query_level = level

    # ── 1. GEO FAST PATH (no cache — koordinat berubah) ──────────────────────
    _lat, _lon, _radius_km = _resolve_geo_params(query, lat, lon, radius_km)
    if (_lat is not None and _lon is not None) or detect_geo_query(query):
        return await _run_geo_fastpath(
            query, es_client, _lat, _lon, _radius_km, start_time, user_id, on_step)

    # ── 1.5. GLOBAL CACHE CHECK ───────────────────────────────────────────────
    # Dilakukan setelah geo bypass agar geo query tidak ter-cache.
    # RAG fallback punya cache internal sendiri; routes lain dicache di sini.
    _global_cache_key = build_cache_key(query, level)
    _cached_resp = await get_cache(_global_cache_key)
    if _cached_resp:
        logger.info("[CACHE] hit key=%s query=%r", _global_cache_key[:24], query[:60])
        _cached_resp["from_cache"]  = True
        _cached_resp["duration_ms"] = 0
        return {**_cached_resp, "_bg_tasks": [], "_bg_queue": [], "_bg_trigger": None}

    # Helper: enqueue cache save untuk hasil confident
    def _enqueue_cache(res: dict) -> dict:
        if res and res.get("answer_confident") and not res.get("from_cache"):
            payload = {k: v for k, v in res.items() if not k.startswith("_")}
            res.setdefault("_bg_queue", []).append(
                (set_cache, [_global_cache_key, payload, 600], {})
            )
        return res

    # ── 2. FAST OOT GUARD ─────────────────────────────────────────────────────
    if is_clearly_oot(query) and not is_education_query(query):
        logger.info("[FAST_OOT] triggered | query=%r", query[:80])
        answer = await narrate_out_of_context(query)
        return _base(query, start_time, user_id, answer, "SYSTEM_OUT_OF_CONTEXT",
                     answer_confident=True, confidence_score=0.7)

    # ── 3. CLASSIFY INTENT ────────────────────────────────────────────────────
    intent, _ = await _run_classification(query, on_step)

    # ── 4. OOT GUARD (post-classify) ──────────────────────────────────────────
    if intent.primary == "out_of_context" and not intent.secondary:
        if not is_education_query(query):
            answer = build_reject_answer()
            return _base(query, start_time, user_id,
                         answer["answer_markdown"], answer["provider"])
        answer = await narrate_out_of_context(query)
        return _base(query, start_time, user_id, answer, "SYSTEM_OUT_OF_CONTEXT",
                     answer_confident=True, confidence_score=0.7)

    # ── 5. MULTI MODULE ORCHESTRATION ─────────────────────────────────────────
    if intent.secondary:
        await _do_step(on_step, "searching", "Mencari data dari beberapa modul...")
        orch_result = await orchestrate(
            intent=intent, query=query, es_client=es_client, user_id=user_id,
            filters=intent.filters, list_handler=list_handler, profile_handler=profile_handler,
        )
        await _do_step(on_step, "narrating", "Menyusun jawaban multi-modul...")
        merged_md = await narrate_multi_module(
            query=query, merged_context=orch_result["merged_context"], intent=intent,
        )
        return _enqueue_cache(_base(
            query, start_time, user_id, merged_md,
            "+".join(orch_result["providers"]),
            num_docs=orch_result["total_docs"],
            confidence_score=orch_result["confidence"],
            context_sufficiency_score=orch_result["confidence"],
            answer_confident=orch_result["confidence"] >= 0.6))

    route = QueryRoute(intent.primary)

    # ── 6. COMPARISON ──────────────────────────────────────────────────────────
    if route == QueryRoute.COMPARISON:
        result = await _handle_comparison_route(
            query, intent, es_client, profile_handler, start_time, user_id, on_step)
        if result is not None:
            return _enqueue_cache(result)
        route = QueryRoute.LIST  # fall-through

    # ── 7. AGGREGATION ─────────────────────────────────────────────────────────
    if route == QueryRoute.AGGREGATION:
        result = await _handle_aggregation_route(
            query, intent, es_client, user_id, start_time, on_step)
        if result is not None:
            return _enqueue_cache(result)
        route = QueryRoute.LIST  # fall-through

    # ── 8. PROFILE ─────────────────────────────────────────────────────────────
    if route == QueryRoute.PROFILE:
        return _enqueue_cache(await _handle_profile_route(
            query, intent, profile_handler, start_time, user_id, on_step))

    # ── 9. LIST ────────────────────────────────────────────────────────────────
    if route in (QueryRoute.LIST, QueryRoute.PROFILE):
        result = await _handle_list_route(
            query, intent, list_handler, start_time, user_id, on_step)
        if result is not None:
            return _enqueue_cache(result)

    # ── 10. RAG FALLBACK (has its own internal cache) ─────────────────────────
    return await _handle_rag_fallback(
        query, intent, query_level, es_client, user_id, start_time, on_step)
