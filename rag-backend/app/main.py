# rag_server.py
 
import app.config as config
import os
import re
import logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
 
import asyncio
import hashlib
import warnings
from pathlib import Path
from typing import List, Optional, Dict
from datetime import datetime
from contextlib import asynccontextmanager
 
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
 
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import ConnectionError as ESConnectionError
 
# ── ROUTING: satu sistem, LLM-based ──────────────────────────────────────────
from app.services.core.cognitive_router import (
    classify_intent,
    QueryRoute,
    is_education_query,
)
from app.services.core.intent_classifier import (
    classify_intent_rule as rule_classify,
    IntentType as RuleIntentType,
    is_clearly_oot,
)
from app.services.core.multi_intent_orchestrator import orchestrate
from app.services.core.school_name_normalizer import normalize_school_name
from app.services.core.result_model import ResultStatus
 
# ── HANDLERS ─────────────────────────────────────────────────────────────────
from app.services.query_handlers.list_school_handler import ListSchoolHandler
from app.services.query_handlers.aggregation_handler import (
    handle_aggregation_query,
    detect_ranking_level,
)
from app.services.query_handlers.school_vs_handler import (
    is_school_vs_school_query,
    handle_school_vs_school,
    format_school_comparison_markdown,
    narrate_school_comparison,
)
from app.services.query_handlers.profile_handler import ProfileHandler
from app.services.query_handlers.comparison_handler import handle_comparison_query
 
# ── NARRATOR ──────────────────────────────────────────────────────────────────
from app.services.narrator_service import (
    narrate_out_of_context,
    narrate_aggregation,
    narrate_comparison_lokasi,
    narrate_comparison_status,
    narrate_school_list,
    narrate_school_profile,
    detect_level,
    narrate_multi_module,
)
 
# ── FORMATTER ─────────────────────────────────────────────────────────────────
from app.services.response_formatter import (
    format_school_answer,
    format_school_list_answer,
    build_reject_answer,
    format_clarification_answer,
    build_location_label,
)
 
# ── SEARCH & GEO ──────────────────────────────────────────────────────────────
from app.services.search_service import (
    perform_search,
    compute_context_sufficiency_score,
    perform_geo_search,
    detect_geo_query,
    geocode_place_name,
    extract_place_from_query,
)
 
# ── INFRA ─────────────────────────────────────────────────────────────────────
from app.services.location_service import initialize_location_index
from app.services.rate_limit_service import check_rate_limit
from app.services.embedding_service import get_embedding_model
from app.services.confidence_engine import compute_rag_confidence, compute_aggregation_confidence
from app.router import route_with_fallback
from app.providers.deepseek import close_deepseek_client
from app.utils.cache import get_cache, set_cache, get_chat_history, save_chat_history
from app.utils.markdown_to_html import markdown_to_safe_html
from app.utils.rag_evaluation_logger import log_rag_evaluation
from app.background.trigger import trigger_background_jobs
 
# =================================================
# CONFIG
# =================================================
 
logger = logging.getLogger(__name__)
 
warnings.filterwarnings(
    "ignore",
    message="'style' attribute specified, but css_sanitizer not set",
    category=UserWarning,
    module="bleach"
)
 
es_client = AsyncElasticsearch(
    hosts=[os.getenv("ELASTICSEARCH_NODE")],
    basic_auth=(
        os.getenv("ELASTICSEARCH_USERNAME"),
        os.getenv("ELASTICSEARCH_PASSWORD"),
    ),
    request_timeout=10,
    max_retries=3,
    retry_on_timeout=True
)
 
profile_handler = ProfileHandler(es_client)
 
list_handler = ListSchoolHandler(
    es_client,
    os.getenv("ELASTICSEARCH_INDEX")
)
 
 
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting application...")
    try:
        get_embedding_model()
        await initialize_location_index(es_client)
        logger.info("All services initialized")
    except Exception as e:
        logger.warning(f"Startup init failed: {e}")
    yield
    logger.info("Shutting down...")
    await asyncio.sleep(1)
    try:
        await es_client.close()
        logger.info("Elasticsearch closed")
    except Exception as e:
        logger.warning(f"Error closing ES: {e}")
    try:
        await close_deepseek_client()
        logger.info("DeepSeek client closed")
    except Exception as e:
        logger.warning(f"Error closing DeepSeek client: {e}")
 
 
# =================================================
# APP INIT
# =================================================
 
app = FastAPI(
    title="RAG API Server",
    version="2.0.0",
    lifespan=lifespan,
)
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
 
# =================================================
# MODELS
# =================================================
 
class AskRequest(BaseModel):
    question: str
    user_id: Optional[str] = None
    level: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_km: Optional[float] = None
 
 
class AskResponse(BaseModel):
    query: str
    answer_markdown: str
    answer_html: str
    provider: str
    from_cache: bool
    duration_ms: int
    context_used: List[str]
    context_sufficiency_score: float
    answer_confident: bool
    confidence_score: Optional[float] = None
    completeness_score: Optional[float] = None
    user_id: str
    num_docs: int
 
 
# =================================================
# HELPERS
# =================================================
 
def build_cache_key(query: str, level: Optional[str]) -> str:
    raw = f"{query}:{level}"
    return f"response:{hashlib.md5(raw.encode()).hexdigest()}"
 
 
def detect_query_level(query: str) -> Optional[str]:
    q = query.lower()
    patterns = {
        "SD":  r"\b(sd|sekolah dasar)\b",
        "SMP": r"\b(smp|sekolah menengah pertama)\b",
        "SMA": r"\b(sma|smk|sekolah menengah atas)\b",
    }
    for level, p in patterns.items():
        if re.search(p, q):
            return level
    return None
 
 
def _extract_radius_from_query(query: str) -> Optional[float]:
    q = query.lower()
    km_patterns = [
        r'radius\s+(\d+(?:\.\d+)?)\s*km',
        r'dalam\s+radius\s+(\d+(?:\.\d+)?)\s*km',
        r'(\d+(?:\.\d+)?)\s*km\b',
        r'(\d+(?:\.\d+)?)\s*kilometer',
    ]
    for pat in km_patterns:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1))
            if 0.1 <= val <= 50:
                return val
    meter_patterns = [
        r'radius\s+(\d+(?:\.\d+)?)\s*(?:m\b|meter)',
        r'dalam\s+(\d+(?:\.\d+)?)\s*(?:m\b|meter)',
        r'(\d+(?:\.\d+)?)\s*meter',
    ]
    for pat in meter_patterns:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1)) / 1000
            if 0.05 <= val <= 5:
                return round(val, 2)
    return None
 
 
def _ms(start: datetime) -> int:
    """Hitung durasi dalam ms dari start hingga sekarang."""
    return int((datetime.now() - start).total_seconds() * 1000)
 
 
def _base_response(
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
) -> AskResponse:
    """
    Helper — bangun AskResponse lengkap dengan semua field wajib.
    Menghindari repetisi 12-field di setiap return statement.
    """
    md = answer_markdown or ""
    return AskResponse(
        query=query,
        answer_markdown=md,
        answer_html=markdown_to_safe_html(md),
        provider=provider,
        from_cache=from_cache,
        duration_ms=_ms(start),
        context_used=context_used or [],
        context_sufficiency_score=context_sufficiency_score,
        answer_confident=answer_confident,
        confidence_score=confidence_score,
        completeness_score=completeness_score,
        user_id=user_id,
        num_docs=num_docs,
    )
 

def _extract_ambiguous_message(result: dict) -> str | None:
    """Ekstrak pesan AMBIGUOUS dari result handler manapun."""
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    # ResultStatus.AMBIGUOUS bisa berupa enum atau string
    is_ambiguous = (
        status == ResultStatus.AMBIGUOUS
        or status == "AMBIGUOUS"
        or str(status) == "ResultStatus.AMBIGUOUS"
    )
    if not is_ambiguous:
        return None
    data = result.get("data", {})
    # Cek message langsung
    if data.get("message"):
        return data["message"]
    # Bangun dari candidates
    candidates = data.get("candidates", [])
    if candidates:
        lines = ["Wilayah yang Anda maksud memiliki beberapa kemungkinan:"]
        for i, c in enumerate(candidates, 1):
            lines.append(f"{i}. {c.get('label', '').title()}")
        lines.append("\nSilakan sebutkan lebih spesifik.")
        return "\n".join(lines)
    return "Wilayah yang Anda maksud ambigu. Silakan sebutkan lebih spesifik."

# =================================================
# ENDPOINT
# =================================================
 
@app.post("/ask", response_model=AskResponse)
async def ask_question(
    request: AskRequest,
    background_tasks: BackgroundTasks,
    req: Request
):
    start_time = datetime.now()
 
    query = normalize_school_name(request.question)
    user_id = request.user_id or (req.client.host if req.client else "anonymous")
    query_level = request.level or detect_query_level(query)
 
    # ── RATE LIMIT ────────────────────────────────────────────────────────────
    limit_status = await check_rate_limit(user_id)
    if not limit_status or not limit_status.get("allowed"):
        raise HTTPException(429, "Rate limit exceeded")
 
    # =================================================
    # GEO SEARCH FAST PATH
    # =================================================
    _lat = request.lat
    _lon = request.lon
    _radius_km = request.radius_km or 2.0
 
    if request.radius_km is None:
        _extracted_radius = _extract_radius_from_query(query)
        if _extracted_radius:
            _radius_km = _extracted_radius
            logger.info("[GEO] Radius diekstrak dari teks: %.1f km", _radius_km)
 
    if _lat is None or _lon is None:
        _coord_match = re.search(
            r'(-?\d{1,3}(?:\.\d+)?)\s*[/,\s]\s*(-?\d{1,3}(?:\.\d+)?)',
            query
        )
        if _coord_match:
            try:
                _lat_c = float(_coord_match.group(1))
                _lon_c = float(_coord_match.group(2))
                if -90 <= _lat_c <= 90 and -180 <= _lon_c <= 180:
                    _lat, _lon = _lat_c, _lon_c
                    logger.info("[GEO] Koordinat diekstrak: lat=%.4f lon=%.4f", _lat, _lon)
            except (ValueError, TypeError):
                pass
 
    if (_lat is None or _lon is None) and detect_geo_query(query):
        _place_name = extract_place_from_query(query)
        if _place_name:
            _geocoded = await geocode_place_name(_place_name)
            if _geocoded:
                _lat, _lon = _geocoded
                logger.info("[GEO] Geocoded: '%s' → lat=%.4f lon=%.4f", _place_name, _lat, _lon)
 
    if (_lat is not None and _lon is not None) or detect_geo_query(query):
        if _lat is not None and _lon is not None:
            # Rule-based extraction — intent belum tersedia di fast path ini
            _JENJANG_MAP_GEO = {
                "sd": "SD", "sdn": "SD", "mi": "MI",
                "smp": "SMP", "smpn": "SMP", "mts": "MTs",
                "sma": "SMA", "sman": "SMA", "ma": "MA",
                "smk": "SMK", "smkn": "SMK", "tk": "TK",
                "tkn": "TK", "paud": "PAUD", "kb": "KB", "slb": "SLB",
            }
            _q_lower_geo = query.lower()
            _geo_bentuk = next(
                (v for k, v in _JENJANG_MAP_GEO.items()
                if re.search(r'\b' + k + r'\b', _q_lower_geo)),
                None
            )
            _has_negeri = bool(re.search(r'\bnegeri\b', _q_lower_geo))
            _has_swasta = bool(re.search(r'\bswasta\b', _q_lower_geo))
            _geo_status = None
            if _has_negeri and not _has_swasta:
                _geo_status = "NEGERI"
            elif _has_swasta and not _has_negeri:
                _geo_status = "SWASTA"

            # Ekstrak akreditasi dari query
            _akred_match = re.search(r'\bakreditasi\s+([abc])\b', _q_lower_geo, re.I)
            _akred_match2 = re.search(r'\bterakreditasi\s+([abc])\b', _q_lower_geo, re.I)
            _geo_akred = None
            if _akred_match:
                _geo_akred = _akred_match.group(1).upper()
            elif _akred_match2:
                _geo_akred = _akred_match2.group(1).upper()
            
            _geo_result = await perform_geo_search(
                es_client=es_client,
                lat=_lat, lon=_lon,
                radius_km=_radius_km,
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
                    f"Tidak ditemukan sekolah dalam radius **{_radius_km} km** "
                    f"dari koordinat yang diberikan"
                )
                if _geo_bentuk:
                    _empty_msg += f" dengan jenjang **{_geo_bentuk}**"
                if _geo_status:
                    _empty_msg += f" berstatus **{_geo_status}**"
                _empty_msg += ". Coba perluas radius pencarian."
                return _base_response(
                    query, start_time, user_id, _empty_msg,
                    "ELASTICSEARCH_GEO",
                    context_used=[f"geo:{_lat},{_lon}"],
                )
 
            _schools = []
            for h in _geo_hits:
                s = h["_source"]
                _schools.append({
                    "nama":           s.get("nama"),
                    "npsn":           s.get("npsn"),
                    "bentuk":         s.get("bentuk_pendidikan"),
                    "status":         s.get("status_sekolah"),
                    "akreditasi":     s.get("akreditasi"),
                    "desa_kelurahan": s.get("desa_kelurahan"),
                    "kecamatan":      s.get("kecamatan"),
                    "kabupaten":      s.get("kabupaten"),
                    "provinsi":       s.get("provinsi"),
                    "jarak_km":       h.get("jarak_km"),
                })
 
            _displayed = len(_schools)
            _info_total = f"Menampilkan **{_displayed}** dari **{_total_in_radius}** sekolah dalam radius {_radius_km} km."
            _lines = [f"### Sekolah Terdekat (radius {_radius_km} km)\n", _info_total, ""]
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
            _bd_jenjang = _breakdown_jenjang
 
            # ── GEO: status note dinamis dari data TOTAL dalam radius ──────────────
            _negeri_total = _bd_status.get("NEGERI", 0) + _bd_status.get("Negeri", 0)
            _swasta_total = _bd_status.get("SWASTA", 0) + _bd_status.get("Swasta", 0)
            _total_ns     = _negeri_total + _swasta_total
            _user_asks_status = bool(
                re.search(r"\b(negeri|swasta|status)\b", query, re.I)
            )
            _status_note = ""
            if _total_ns > 0:
                _pct_negeri = round(_negeri_total / _total_ns * 100)
                if _negeri_total == 0:
                    _status_note = (
                        f"\u26a0\ufe0f Dalam radius {_radius_km} km ini "
                        f"**tidak ditemukan sekolah berstatus NEGERI**. "
                        f"Seluruh {_total_in_radius:,} sekolah yang terdeteksi berstatus swasta."
                    )
                elif _user_asks_status or _swasta_total > _negeri_total * 2:
                    _status_note = (
                        f"\u2139\ufe0f Dari **{_total_in_radius:,} sekolah** dalam radius ini: "
                        f"**{_negeri_total:,} Negeri** ({_pct_negeri}%) dan "
                        f"**{_swasta_total:,} Swasta** ({100 - _pct_negeri}%). "
                        + ("Area ini didominasi sekolah swasta." if _swasta_total > _negeri_total else "")
                    )
            if _status_note:
                _template_md = f"{_status_note}\n\n{_template_md}"
            # ─────────────────────────────────────────────────────────────────────
 
            _geo_active_filters = {}
            if _geo_bentuk:
                _geo_active_filters["bentuk"] = _geo_bentuk
            if _geo_status:
                _geo_active_filters["status"] = _geo_status
            if _geo_akred:
                _geo_active_filters["akreditasi"] = _geo_akred
 
            _md = await narrate_school_list(
                wilayah=f"radius {_radius_km} km dari koordinat {_lat:.4f},{_lon:.4f}",
                total=_total_in_radius,
                schools=_schools,
                template_md=_template_md,
                breakdown_jenjang=_bd_jenjang,
                breakdown_status=_bd_status,
                active_filters=_geo_active_filters,
            )

            _geo_context = [f"geo:{_lat},{_lon}"]
            if _geo_bentuk:
                _geo_context.append(f"bentuk:{_geo_bentuk}")
            if _geo_akred:
                _geo_context.append(f"akreditasi:{_geo_akred}")
            if _geo_status:
                _geo_context.append(f"status:{_geo_status}")

            
            return _base_response(
                query, start_time, user_id, _md,
                "ELASTICSEARCH_GEO",
                context_used=_geo_context,
                context_sufficiency_score=0.9,
                answer_confident=True,
                confidence_score=0.9,
                num_docs=len(_schools),
            )
 
        else:
            _geo_msg = (
                "Untuk mencari sekolah terdekat, saya membutuhkan koordinat lokasi Anda.\n\n"
                "Silakan kirimkan parameter `lat` dan `lon` bersama pertanyaan. Contoh:\n"
                "```json\n{\n"
                '  "question": "sekolah terdekat dari sini",\n'
                '  "lat": -6.2088,\n'
                '  "lon": 106.8456,\n'
                '  "radius_km": 3\n'
                "}\n```"
            )
            return _base_response(
                query, start_time, user_id, _geo_msg,
                "SYSTEM_GEO_NO_COORDS",
            )
 
    # =================================================
    # ROUTE RESOLUTION V2 (multi-intent aware)
    # =================================================
    # =================================================
    # FAST OOT GUARD (SEBELUM LLM)
    # =================================================
    if not detect_geo_query(query):
        if is_clearly_oot(query):
            if not is_education_query(query):
                logger.info("[FAST_OOT] triggered | query=%r", query[:80])
                answer = await narrate_out_of_context(query)
                return _base_response(
                    query,
                    start_time,
                    user_id,
                    answer,
                    "SYSTEM_OUT_OF_CONTEXT",
                    answer_confident=True,
                    confidence_score=0.7,
                )
            else:
                logger.info("[FAST_OOT] skipped due to education signal")
    # Ganti timeout per-call menjadi adaptif
    intent = await classify_intent(
        query,
        timeout=11.0,   
        use_cache=True
    )
 
    logger.info(
        "[RouterV2] primary=%s secondary=%s | query=%r",
        intent.primary,
        intent.secondary,
        query[:80],
    )
 
    # =================================================
    # ADJUDICATION — rekonsiliasi LLM vs rule-based classifier
    # =================================================
    #
    # Masalah: classify_intent (LLM) kadang mengembalikan "out_of_context"
    # untuk query yang sebenarnya valid (ambiguous, typo, query campuran),
    # terutama saat LLM fallback ke keyword_fallback.
    #
    # Strategi: jalankan rule-based classifier (O(1), tanpa LLM) sebagai
    # second opinion. Jika rule-based yakin query ini SCHOOL tapi LLM bilang
    # OOT → override ke "list" sebagai modul paling aman.
    #
    # Override hanya terjadi pada konflik OOT vs SCHOOL — kita tidak
    # mengganggu klasifikasi modul spesifik (aggregation/comparison/profile/geo).
 
    if intent.primary == "out_of_context" and not intent.secondary:
        rule_result = rule_classify(query)
        if rule_result == RuleIntentType.SCHOOL:
            logger.info(
                "[Adjudication] LLM→OOT tapi rule→SCHOOL, override ke LIST | query=%r",
                query[:80],
            )
            intent.primary   = "list"
            intent.is_oot    = False
            intent.oot_reason = None
        elif rule_result == RuleIntentType.AMBIGUOUS:
            # AMBIGUOUS: LLM tidak yakin OOT dan rule pun ragu → override ke
            # list hanya jika query mengandung sinyal lokasi atau jenjang
            _q = query.lower()
            _has_edu_signal = any(
                kw in _q for kw in (
                    "sekolah", "sd", "smp", "sma", "smk", "tk", "paud",
                    "guru", "siswa", "akreditasi", "npsn",
                )
            )
            if _has_edu_signal:
                logger.info(
                    "[Adjudication] LLM→OOT, rule→AMBIGUOUS + sinyal edu, override ke LIST",
                )
                intent.primary   = "list"
                intent.is_oot    = False
                intent.oot_reason = None
 
    # =================================================
    # OUT OF CONTEXT GUARD
    # =================================================
 
    if intent.primary == "out_of_context" and not intent.secondary:
        # Domain filter cadangan: jika lolos OOC tapi bukan query pendidikan
        if not is_education_query(query):
            answer = build_reject_answer()
            return _base_response(
                query, start_time, user_id,
                answer["answer_markdown"],
                answer["provider"],
            )
 
        answer = await narrate_out_of_context(query)
        return _base_response(
            query, start_time, user_id, answer,
            "SYSTEM_OUT_OF_CONTEXT",
            answer_confident=True,
            confidence_score=0.7,
        )
    # =================================================
    # MULTI MODULE ORCHESTRATION
    # =================================================
 
    if intent.secondary:
 
        orch_result = await orchestrate(
            intent=intent,
            query=query,
            es_client=es_client,
            user_id=user_id,
            filters=intent.filters,
        )
 
        merged_md = await narrate_multi_module(
            query=query,
            merged_context=orch_result["merged_context"],
            intent=intent,
        )
 
        return _base_response(
            query,
            start_time,
            user_id,
            merged_md,
            "+".join(orch_result["providers"]),
            num_docs=orch_result["total_docs"],
            confidence_score=orch_result["confidence"],
            context_sufficiency_score=orch_result["confidence"],
            answer_confident=orch_result["confidence"] >= 0.6,
        )
    # fallback ke single route lama
    route = QueryRoute(intent.primary)
    # =================================================
    # COMPARISON
    # =================================================
 
    if route == QueryRoute.COMPARISON:
        # ── School vs School guard — intercept sebelum comparison handler ──
        if is_school_vs_school_query(query):
            logger.info("[SCHOOL_VS] Detected school vs school in COMPARISON route")
            vs_result = await handle_school_vs_school(query, profile_handler)
            if vs_result and vs_result.get("status") == "success":
                comparison_md = format_school_comparison_markdown(vs_result)
                comparison_md = await narrate_school_comparison(vs_result, comparison_md)
                return _base_response(
                    query, start_time, user_id,
                    comparison_md, "ELASTICSEARCH_SCHOOL_VS",
                    context_used=[
                        f"npsn:{vs_result['school_a'].get('data', {}).get('identitas', {}).get('npsn', '')}",
                        f"npsn:{vs_result['school_b'].get('data', {}).get('identitas', {}).get('npsn', '')}",
                    ],
                    context_sufficiency_score=1.0,
                    answer_confident=True,
                    confidence_score=0.90,
                    num_docs=2,
                )
            # Jika school_vs gagal (sekolah tidak ditemukan), lanjut ke comparison handler normal

        try:
            comparison_result = await handle_comparison_query(
                query=query,
                es_client=es_client
            )
            _amb_msg = _extract_ambiguous_message(comparison_result)
            if _amb_msg:
                return _base_response(
                    query, start_time, user_id,
                    _amb_msg,
                    "SYSTEM_AMBIGUOUS",
                    answer_confident=False,
                    confidence_score=0.5,
                )
        except Exception:
            logger.exception("[COMPARISON] handler crashed")
            raise HTTPException(500, "Comparison internal error")
 
        if comparison_result and isinstance(comparison_result, dict):
            provider = comparison_result.get("provider", "")
 
            # ── System error ─────────────────────────────────────────
            if provider == "SYSTEM_ERROR":
                return _base_response(
                    query, start_time, user_id,
                    "Terjadi kesalahan dalam memproses perbandingan.",
                    "SYSTEM_ERROR",
                )
 
            # ── Validation (minimal 2 lokasi, hierarki tidak lengkap) ─
            if provider == "SYSTEM_VALIDATION":
                reason = comparison_result.get("reason", "")
                if reason == "COMPARISON_NEEDS_MULTIPLE_LOCATIONS":
                    answer_markdown = (
                        "Untuk perbandingan, sebutkan minimal dua lokasi.\n\n"
                        "Contoh: _\"bandingkan cisauk vs parungpanjang\"_"
                    )
                elif reason == "HIERARCHY_INCOMPLETE":
                    answer_markdown = "Lokasi tidak lengkap atau tidak dikenali."
                else:
                    answer_markdown = "Lokasi tidak dikenali. Coba perjelas nama wilayah."
                return _base_response(
                    query, start_time, user_id,
                    answer_markdown, "SYSTEM_VALIDATION",
                )
 
            # ── Status comparison (negeri vs swasta) ──────────────────
            if provider == "ELASTICSEARCH_COMPARISON_STATUS":
                answer_markdown = comparison_result.get("answer_markdown", "")
                total_hits      = comparison_result.get("num_docs", 0)
                confidence      = comparison_result.get("confidence", 0.0)
                results_raw     = comparison_result.get("results", [])
 
                counts    = {status.lower(): count for status, count in results_raw}
                negeri    = counts.get("negeri", 0)
                swasta    = counts.get("swasta", 0)
                pct_diff  = 0
                if min(negeri, swasta) > 0:
                    dominan     = max(negeri, swasta)
                    non_dominan = min(negeri, swasta)
                    pct_diff    = round((dominan - non_dominan) / non_dominan * 100)
 
                wilayah_match = re.search(r"\*\*Wilayah:\s*(.+?)\*\*", answer_markdown)
                wilayah_label = wilayah_match.group(1) if wilayah_match else "wilayah ini"
 
                answer_markdown = await narrate_comparison_status(
                    wilayah=wilayah_label,
                    negeri=negeri,
                    swasta=swasta,
                    pct_diff=pct_diff,
                    template_md=answer_markdown,
                )
                return _base_response(
                    query, start_time, user_id,
                    answer_markdown, "ELASTICSEARCH_COMPARISON_STATUS",
                    context_sufficiency_score=1.0,
                    answer_confident=comparison_result.get("answer_confident", False),
                    confidence_score=confidence,
                    num_docs=total_hits,
                )
 
            # ── Jenjang comparison (sma vs smk di satu wilayah) ───────
            if provider == "ELASTICSEARCH_COMPARISON_JENJANG":
                answer_markdown = comparison_result.get("answer_markdown", "")
                total_hits      = comparison_result.get("num_docs", 0)
                confidence      = comparison_result.get("confidence", 0.0)
                return _base_response(
                    query, start_time, user_id,
                    answer_markdown, "ELASTICSEARCH_COMPARISON_JENJANG",
                    context_sufficiency_score=1.0,
                    answer_confident=comparison_result.get("answer_confident", False),
                    confidence_score=confidence,
                    num_docs=total_hits,
                )
 
            # ── Normal comparison (dua lokasi berbeda) ─────────────────
            if provider == "ELASTICSEARCH_COMPARISON":
                results         = comparison_result.get("results", [])
                results_detail  = comparison_result.get("results_detail", [])
                answer_markdown = comparison_result.get("answer_markdown", "")
                total_sum       = sum(c for _, c in results)
                confidence      = compute_aggregation_confidence(
                    query=query,
                    resolution_status="SUCCESS",
                    resolution_metadata={},
                    total_hits=total_sum,
                )
 
                if not results:
                    return _base_response(
                        query, start_time, user_id,
                        "Data tidak ditemukan untuk perbandingan.",
                        "SYSTEM_NO_DATA",
                        context_sufficiency_score=1.0,
                        confidence_score=confidence,
                    )
 
                if not answer_markdown:
                    lines = [f"- {loc}: **{count:,} sekolah**" for loc, count in results]
                    answer_markdown = "### Perbandingan Jumlah Sekolah\n\n" + "\n".join(lines)
 
                narrator_results = []
                for i, (loc, count) in enumerate(results):
                    item = {"lokasi": loc, "count": count}
                    if i < len(results_detail):
                        rd = results_detail[i]
                        item["breakdown_jenjang"] = rd.get("breakdown_jenjang", {})
                        item["breakdown_status"]  = rd.get("breakdown_status", {})
                        # Teruskan data avg siswa jika tersedia (untuk query rata-rata)
                        if rd.get("avg_siswa"):
                            item["avg_siswa"]   = round(rd["avg_siswa"])
                        if rd.get("total_siswa"):
                            item["total_siswa"] = rd["total_siswa"]
                        if rd.get("max_siswa"):
                            item["max_siswa"]   = rd["max_siswa"]
                    narrator_results.append(item)
 
                answer_markdown = await narrate_comparison_lokasi(
                    results=narrator_results,
                    template_md=answer_markdown,
                    results_detail=narrator_results,
                    query=query,
                )
 
                context_used_list = []
                for r in (results_detail or []):
                    ent = r.get("entities", {})
                    context_used_list.extend(f"{k}:{v}" for k, v in ent.items() if v)
 
                return _base_response(
                    query, start_time, user_id,
                    answer_markdown, "ELASTICSEARCH_COMPARISON",
                    context_used=context_used_list,
                    context_sufficiency_score=1.0,
                    answer_confident=confidence >= 0.6,
                    confidence_score=confidence,
                    num_docs=total_sum,
                )
 
        # Comparison tidak menghasilkan data → fall through ke LIST
        logger.info("[COMPARISON] no result, fallback → LIST")
        route = QueryRoute.LIST
 
    # =================================================
    # AGGREGATION
    # =================================================
 
    if route == QueryRoute.AGGREGATION:
        try:
            result = await handle_aggregation_query(
                query=query,
                es_client=es_client,
                user_id=user_id,
                intent=intent,
            )
            #  TAMBAHAN: global ambiguous guard
            _amb_msg = _extract_ambiguous_message(result)
            if _amb_msg:
                return _base_response(
                    query, start_time, user_id,
                    _amb_msg,
                    "SYSTEM_AMBIGUOUS",
                    answer_confident=False,
                    confidence_score=0.5,
                )
        except Exception:
            logger.exception("[AGGREGATION] handler crashed")
            raise HTTPException(500, "Aggregation internal error")
 
        if not result:
            route = QueryRoute.LIST
        else:
            agg_provider = result.get("provider", "")
 
            # ── Validation / Error ───────────────────────────────────
            if agg_provider in ("SYSTEM_VALIDATION", "SYSTEM_ERROR"):
                if result.get("reason") == "HIERARCHY_INCOMPLETE":
                    missing = result.get("missing_parent")
                    level   = result.get("level")
                    answer_md = (
                        f"Untuk menentukan **{level}**, "
                        f"Anda perlu menyebutkan terlebih dahulu **{missing}**.\n\n"
                        f"Contoh:\n- Jumlah sekolah di {level} X di {missing} Y"
                    )
                else:
                    answer_md = "**Data tidak ditemukan atau lokasi tidak dikenali.**"
 
                confidence = compute_aggregation_confidence(
                    query=query, resolution_status="FAILED",
                    resolution_metadata=result, total_hits=0
                )
                return _base_response(
                    query, start_time, user_id,
                    answer_md, agg_provider,
                    confidence_score=confidence,
                )
 
            # ── Global ranking ───────────────────────────────────────
            if agg_provider == "ELASTICSEARCH_GLOBAL_RANKING":
                loc   = result.get("top_location")
                total = result.get("total")
                level = detect_ranking_level(query)
                label_map = {
                    "provinsi": "Provinsi", "kabupaten": "Kabupaten/Kota",
                    "kecamatan": "Kecamatan", "desa_kelurahan": "Desa/Kelurahan",
                }
                label     = label_map.get(level, "Wilayah")
                answer_md = (
                    f"{label} dengan jumlah sekolah terbanyak adalah "
                    f"**{loc}** dengan **{total:,} sekolah**."
                )
                confidence = result.get("confidence", 0.0)
                return _base_response(
                    query, start_time, user_id,
                    answer_md, "ELASTICSEARCH_GLOBAL_RANKING",
                    context_used=[f"provinsi:{loc}"],
                    context_sufficiency_score=1.0,
                    answer_confident=confidence >= 0.6,
                    confidence_score=confidence,
                    num_docs=total,
                )
 
            # ── Normal aggregation (single / multi) ──────────────────
            if result.get("is_multi"):
                comparison_list = result.get("comparison", [])
                if not comparison_list:
                    answer_md = "Data tidak ditemukan untuk lokasi yang dimaksud."
                else:
                    comparison_list.sort(key=lambda x: x.get("total", 0), reverse=True)
                    lines = []
                    narrator_results_multi = []
                    for item in comparison_list:
                        ent   = item.get("entities", {})
                        total = item.get("total", 0)
                        label = "Wilayah"
                        for lvl in ["desa_kelurahan", "kecamatan", "kabupaten", "provinsi"]:
                            val = ent.get(lvl)
                            if val:
                                label = (
                                    val.replace("kec.", "Kec.")
                                       .replace("kab.", "Kab.")
                                       .replace("prov.", "Prov.")
                                       .title()
                                )
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
                        results=narrator_results_multi,
                        template_md=answer_md,
                        query=query,
                    )
            else:
                answer_md = format_school_answer(result)
                entities_for_narasi = result.get("entities", {}) or {}
                wilayah_narasi      = build_location_label(entities_for_narasi)
                total_narasi        = result.get("total", 0) or 0
                status_narasi       = entities_for_narasi.get("status_sekolah")
 
                answer_md = await narrate_aggregation(
                    wilayah=wilayah_narasi,
                    total=total_narasi,
                    template_md=answer_md,
                    status_filter=status_narasi,
                    level=detect_level(result.get("entities", {})),
                    breakdown_jenjang=result.get("breakdown_jenjang"),
                    breakdown_status=result.get("breakdown_status"),
                    drilldown_top3=result.get("drilldown_top3"),
                    drilldown_field=result.get("drilldown_field"),
                    total_all=result.get("total_all"),
                    pct_of_total=result.get("pct_of_total"),
                    active_filters=result.get("active_filters"),
                    jenjang_requested=result.get("jenjang_requested"),
                )
 
            # Disclaimer confidence rendah untuk desa
            resolution_metadata = result.get("metadata", {}) or {}
            if resolution_metadata.get("low_confidence_desa"):
                entities = result.get("entities", {})
                desa_val = entities.get("desa_kelurahan", "")
                kab_val  = entities.get("kabupaten", "")
                answer_md += (
                    f"\n\n> **Catatan:** Sistem menemukan satu data untuk "
                    f"'{desa_val.title()}' di {kab_val.title()}. "
                    f"Jika bukan lokasi yang dimaksud, tambahkan nama "
                    f"kabupaten atau kecamatan untuk hasil yang lebih akurat."
                )
 
            confidence_score = result.get("confidence", 0.0)
            answer_confident = result.get("answer_confident", False)
            entities         = result.get("entities", {}) or {}
 
            if result.get("is_multi"):
                comparison_list   = result.get("comparison", [])
                total_docs        = sum(x.get("total", 0) for x in comparison_list)
                context_used_list = []
                for item in comparison_list:
                    ent = item.get("entities", {})
                    context_used_list.extend(f"{k}:{v}" for k, v in ent.items())
            else:
                total_docs        = result.get("total", 0)
                context_used_list = [f"{k}:{v}" for k, v in entities.items()]
 
            return _base_response(
                query, start_time, user_id,
                answer_md, result.get("provider", "UNKNOWN"),
                context_used=context_used_list,
                context_sufficiency_score=float(confidence_score),
                answer_confident=answer_confident,
                confidence_score=confidence_score,
                num_docs=total_docs,
            )
 
    # =================================================
    # PROFILE
    # =================================================
 
    if route == QueryRoute.PROFILE:
 
        # ── School vs School ─────────────────────────────────────────────
        if is_school_vs_school_query(query):
            logger.info("[SCHOOL_VS] Detected school vs school query")
            vs_result = await handle_school_vs_school(query, profile_handler)
 
            if vs_result and vs_result.get("status") == "success":
                comparison_md = format_school_comparison_markdown(vs_result)
                comparison_md = await narrate_school_comparison(vs_result, comparison_md)
                return _base_response(
                    query, start_time, user_id,
                    comparison_md, "ELASTICSEARCH_SCHOOL_VS",
                    context_used=[
                        f"npsn:{vs_result['school_a'].get('data', {}).get('identitas', {}).get('npsn', '')}",
                        f"npsn:{vs_result['school_b'].get('data', {}).get('identitas', {}).get('npsn', '')}",
                    ],
                    context_sufficiency_score=1.0,
                    answer_confident=True,
                    confidence_score=0.90,
                    num_docs=2,
                )
 
        # ── Profile cache check ──────────────────────────────────────────
        profile_cache_key = f"profile:v2:{hashlib.md5(query.encode()).hexdigest()}"
        cached_profile    = await get_cache(profile_cache_key)
 
        if cached_profile:
            if "answer_markdown" not in cached_profile:
                logger.warning("Invalid profile cache format, deleting...")
                await set_cache(profile_cache_key, None, 1)
            else:
                cached_profile["answer_html"]  = markdown_to_safe_html(cached_profile["answer_markdown"])
                cached_profile["from_cache"]   = True
                cached_profile["duration_ms"]  = 0
                return AskResponse(**cached_profile)
 
        profile_result = await profile_handler.handle(query, intent=intent)
        logger.debug("[PROFILE] result status: %s", profile_result.get("status"))
 
        if profile_result["status"] == "not_found":
            return _base_response(
                query, start_time, user_id,
                "Sekolah tidak ditemukan.",
                "ELASTICSEARCH_PROFILE",
                context_sufficiency_score=1.0,
            )
 
        if profile_result["status"] == "ambiguous":
            candidates  = profile_result["candidates"]
            msg         = profile_result.get("message", "Mohon perjelas sekolah yang dimaksud:")
            options_md  = "\n".join(
                f"- {c.get('nama')} \n"
                f"  Lokasi: {c.get('lokasi')} \n"
                f"  NPSN: {c.get('npsn')}"
                for c in candidates
            )
            answer_md = "### Ditemukan Beberapa Sekolah\n\n" + msg + "\n\n" + options_md
            return _base_response(
                query, start_time, user_id,
                answer_md, "ELASTICSEARCH_PROFILE_AMBIGUOUS",
                context_sufficiency_score=1.0,
                num_docs=len(candidates),
            )
 
        # ── Success ──────────────────────────────────────────────────────
        profile_data = profile_result.get("data") or {}
        if not isinstance(profile_data, dict) or not profile_data:
            raise HTTPException(status_code=500, detail="Invalid profile format")
 
        formatted_md = profile_result.get("formatted")
        if not formatted_md:
            from app.services.formatters.profile_formatter import format_profile_markdown
            formatted_md = format_profile_markdown(profile_data)
        formatted_md = await narrate_school_profile(
            profile_data=profile_data,
            template_md=formatted_md,
        )
 
        npsn_value        = profile_data.get("identitas", {}).get("npsn")
        completeness_score = profile_result.get("completeness_score", 0.0)
        confidence_score  = profile_result.get("confidence", 0.0)
 
        response_payload = {
            "query":                    query,
            "answer_markdown":          formatted_md,
            "provider":                 "ELASTICSEARCH_PROFILE",
            "from_cache":               False,
            "context_used":             [f"npsn:{npsn_value}"] if npsn_value else [],
            "context_sufficiency_score": 1.0,
            "answer_confident":         confidence_score >= 0.6,
            "confidence_score":         confidence_score,
            "completeness_score":       completeness_score,
            "user_id":                  user_id,
            "num_docs":                 1,
        }
 
        # TTL dinamis: data >= 2 tahun → 24 jam, data baru → 1 jam
        _tahun_data = profile_data.get("tahun") or profile_data.get("identitas", {}).get("tahun")
        try:
            _tahun_int = int(_tahun_data) if _tahun_data else 0
        except (ValueError, TypeError):
            _tahun_int = 0
        _profile_ttl = 86400 if (_tahun_int > 0 and (datetime.now().year - _tahun_int) >= 2) else 3600
        await set_cache(profile_cache_key, response_payload, _profile_ttl)
 
        return AskResponse(
            **response_payload,
            duration_ms=_ms(start_time),
            answer_html=markdown_to_safe_html(formatted_md),
        )
 
    # =================================================
    # LIST
    # =================================================
 
    if route in (QueryRoute.LIST, QueryRoute.PROFILE):
        # PROFILE bisa fall-through ke sini jika school_vs dan profile handler sama-sama gagal
        result = await list_handler.handle(query, intent=intent)
        _amb_msg = _extract_ambiguous_message(result)
        if _amb_msg:
            return _base_response(
                query, start_time, user_id,
                _amb_msg,
                "SYSTEM_AMBIGUOUS",
                answer_confident=False,
                confidence_score=0.5,
            )
        status_res  = result.get("status")
        
        schools     = result.get("schools", [])
        total       = result.get("total", 0)
 
        if status_res == "clarification":
            formatted = format_clarification_answer(result)
            return _base_response(
                query, start_time, user_id,
                formatted["answer_markdown"], "ELASTICSEARCH_LIST",
                context_sufficiency_score=1.0,
            )
 
        if not schools:
            return _base_response(
                query, start_time, user_id,
                "Data sekolah tidak ditemukan untuk lokasi tersebut.",
                "ELASTICSEARCH_LIST",
                context_sufficiency_score=1.0,
            )
 
        formatted = format_school_list_answer(result)
        wilayah_list = result.get("parsed", {}).get("wilayah", "") or "wilayah yang diminta"
        _parsed = result.get("parsed", {})
        _active_filters = {
            k: v for k, v in {
                "bentuk":     _parsed.get("bentuk"),
                "status":     _parsed.get("status"),
                "akreditasi": _parsed.get("akreditasi"),
            }.items() if v
        }
 
        formatted["answer_markdown"] = await narrate_school_list(
            wilayah=wilayah_list,
            total=total,
            schools=schools,
            template_md=formatted["answer_markdown"],
            breakdown_jenjang=result.get("breakdown_jenjang"),
            breakdown_status=result.get("breakdown_status"),
            active_filters=_active_filters,
            wilayah_level=result.get("wilayah_level", ""),
        )
 
        confidence = compute_aggregation_confidence(
            query=query,
            resolution_status="SUCCESS",
            resolution_metadata=result.get("metadata", {}),
            total_hits=total,
        )
        is_desa_level  = "desa_kelurahan" in (result.get("entities") or {})
        threshold      = 0.5 if is_desa_level else 0.6
        answer_confident = confidence >= threshold
        _context_parts = [f"wilayah:{wilayah_list}"]
        if _parsed.get("bentuk"):
            _context_parts.append(f"bentuk_pendidikan:{_parsed['bentuk']}")
        if _parsed.get("status"):
            _context_parts.append(f"status:{_parsed['status']}")
        if _parsed.get("akreditasi"):
            _context_parts.append(f"akreditasi:{_parsed['akreditasi']}")

        return _base_response(
            query, start_time, user_id,
            formatted["answer_markdown"], "ELASTICSEARCH_LIST",
            context_used=_context_parts,
            context_sufficiency_score=float(confidence),
            answer_confident=answer_confident,
            confidence_score=confidence,
            num_docs=total,
        )
 
    # =================================================
    # RAG FLOW — fallback terakhir
    # =================================================
 
    cache_key = build_cache_key(query, query_level)
    cached = await get_cache(cache_key)
    if cached:
        cached["answer_html"] = markdown_to_safe_html(cached["answer_markdown"])
        cached["from_cache"]  = True
        cached["duration_ms"] = 0
        return AskResponse(**cached)
 
    history = await get_chat_history(user_id) or []
    history_context = "\n".join(
        f"{m['role']}: {m['content']}" for m in history[-3:]
    )
    # Bangun location filter dari intent untuk RAG path
    _loc_filter: Dict[str, str] = {}
    if intent:
        _w = getattr(intent, "wilayah", None)
        if _w:
            if getattr(_w, "provinsi", None):
                _loc_filter["provinsi"] = _w.provinsi
            if getattr(_w, "kabupaten", None):
                _loc_filter["kabupaten"] = _w.kabupaten
            if getattr(_w, "kecamatan", None):
                _loc_filter["kecamatan"] = _w.kecamatan
        # Gunakan level paling spesifik yang tersedia — jangan stack semua level
        # agar tidak terlalu restrictive
        if "kecamatan" in _loc_filter:
            _loc_filter = {"kecamatan": _loc_filter["kecamatan"]}
        elif "kabupaten" in _loc_filter:
            _loc_filter = {"kabupaten": _loc_filter["kabupaten"]}
        elif "provinsi" in _loc_filter:
            _loc_filter = {"provinsi": _loc_filter["provinsi"]}

    hits = await perform_search(
        es_client=es_client,
        query_text=query,
        size=5,
        filter_level=query_level,
        location_filter=_loc_filter or None,
    )
 
    retrieval_score = compute_context_sufficiency_score(hits)
    context_parts   = [
        f"[SUMBER {i+1}]: {h.get('_source', {}).get('content', '')}"
        for i, h in enumerate(hits)
    ]
    context = "\n\n".join(context_parts) if hits else "Tidak ada data relevan."
 
    if retrieval_score < 0.3:
        return _base_response(
            query, start_time, user_id,
            "Data tidak cukup relevan untuk menjawab pertanyaan.",
            "SYSTEM_LOW_RETRIEVAL",
            num_docs=len(hits),
        )
 
    ai_result  = await route_with_fallback(
        query=query,
        initial_context=context,
        history_context=history_context,
    )
    evaluation = ai_result.get("evaluation") or {}
    llm_score  = evaluation.get("score", 0)
 
    if llm_score < 30 and retrieval_score < 0.4:
        return _base_response(
            query, start_time, user_id,
            "Data yang tersedia belum cukup kuat untuk memberikan jawaban yang akurat. "
            "Silakan perjelas pertanyaan atau lokasi yang dimaksud.",
            "SYSTEM_LOW_CONTEXT",
            context_sufficiency_score=float(llm_score),
            num_docs=len(hits),
        )
 
    final_response   = ai_result["response"]
    confidence_score = compute_rag_confidence(
        llm_score=llm_score,
        retrieval_score=retrieval_score,
        num_docs=len(hits),
    )
    answer_confident = confidence_score >= 0.6
 
    response_data = {
        "query":                     query,
        "answer_markdown":           final_response,
        "provider":                  ai_result["provider"],
        "from_cache":                False,
        "duration_ms":               _ms(start_time),
        "context_used":              [],
        "context_sufficiency_score": float(llm_score),
        "answer_confident":          answer_confident,
        "confidence_score":          confidence_score,
        "user_id":                   user_id,
        "num_docs":                  len(hits),
    }
 
    background_tasks.add_task(set_cache, cache_key, response_data, 600)
    background_tasks.add_task(save_chat_history, user_id, "user", query)
    background_tasks.add_task(save_chat_history, user_id, "assistant", final_response)
    background_tasks.add_task(
        log_rag_evaluation,
        query=query,
        duration_ms=response_data["duration_ms"],
        context_sufficiency_score=llm_score,
        answer_confident=answer_confident,
        num_docs=len(hits),
        answer_length=len(final_response),
    )
 
    await trigger_background_jobs(response_data, background_tasks)
 
    return AskResponse(
        **response_data,
        answer_html=markdown_to_safe_html(final_response),
    )
 
 
# =================================================
# ANALYTICS / FEEDBACK LOOP ENDPOINTS
# =================================================
 
@app.get("/report/weekly")
async def weekly_report(days: int = 7, top_n: int = 20):
    from app.background.analytics import generate_weekly_report
    return generate_weekly_report(days=days, top_n=top_n)
 
 
@app.get("/report/ooc-signals")
async def ooc_signals(top_n: int = 50):
    from app.background.analytics import extract_ooc_keyword_signals
    tokens = extract_ooc_keyword_signals(top_n=top_n)
    return {
        "count": len(tokens),
        "suggestion": "Pertimbangkan token-token ini untuk ditambahkan ke OOC_KEYWORDS",
        "tokens": tokens,
    }
 
 
@app.get("/report/ooc-alerts")
async def ooc_alerts():
    import csv
    log_file = Path(__file__).parent / "logs" / "ooc_alert_log.csv"
    if not log_file.exists():
        return {"total": 0, "alerts": []}
    alerts = []
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                alerts.append(row)
    except Exception as e:
        return {"error": str(e)}
    return {"total": len(alerts), "alerts": alerts[-50:]}
 
 
@app.get("/health")
async def health_check():
    try:
        await es_client.ping(request_timeout=2)
        return {"status": "UP"}
    except ESConnectionError:
        return JSONResponse(status_code=503, content={"status": "DOWN"})
 
 
# ========== MAIN ==========
if __name__ == "__main__":
    import uvicorn
 
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
 
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "server.log"),
            logging.StreamHandler(),
        ]
    )
 
    port = int(os.getenv("PORT", 3000))
    uvicorn.run(
        "rag_server:app",
        host="0.0.0.0",
        port=port,
        reload=(os.getenv("PROD") != "production"),
    )
