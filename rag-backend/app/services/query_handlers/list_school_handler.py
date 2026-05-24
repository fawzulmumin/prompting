# list_school_handler.py

import re
import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Set
from elasticsearch import AsyncElasticsearch
from app.services.core.es_query_builder import extract_filters_from_intent, build_qc_filter
from app.services.embedding_service import get_cached_embedding
from app.services.core.jenjang_constants import JENJANG_MAP
from app.services.core.location_resolver import strip_loc_prefix
from app.services.core.list_location_detector import (
    _detect_implicit_multi_wilayah,
    _split_dan_query,
    _extract_multi_wilayah,
    _find_location_candidates,
    _fallback_detect_wilayah,
    _has_multi_level_match,
    _extract_location_keyword,
    _is_multikata_location,
    _is_multikata_with_context,
    _has_province_siblings,
    _resolve_pulau_keyword,
)
from app.services.core.cognitive_router import CognitiveIntent, WilayahEntities
from app.services.core.location_resolver_adapter import PROVINCE_ALIAS_MAP

logger = logging.getLogger(__name__)

from app.services.query_handlers.list_sort_utils import (
    detect_sort_mode as _detect_sort_mode,
    build_sort_clause as _build_sort_clause,
)
from app.services.query_handlers.list_query_builder import (
    LIST_AGGS_SNIPPET,
    extract_breakdowns,
    hybridize_query as _hybridize_query,
    build_list_query,
    match_phrase,
    match_phrase_boosted,
    strip_prefix,
    _PAUD_BENTUK_EXPAND,
)
# ================================================================
# TYPO CORRECTION — delegates to LocationResolver (replaces custom Levenshtein)
# ================================================================

def _apply_typo_correction(kw: str, q_low: str = "") -> Optional[Tuple[str, str]]:
    """Resolve keyword via LocationResolver. Returns (canonical_name, level) or None."""
    if not kw or len(kw) < 4 or " " in kw:
        return None
    try:
        from app.services.core.location_resolver import LocationResolver
    except ImportError:
        return None
    _resolver = LocationResolver()
    for level_attr in ("kecamatan", "kabupaten", "provinsi"):
        class _W:
            provinsi = kabupaten = kecamatan = desa = landmark = None
        w = _W()
        setattr(w, level_attr, kw)
        res = _resolver.resolve(w)
        if res.method != "unresolved":
            val = getattr(res, level_attr)
            if val:
                logger.info("[LIST] Resolver corrected '%s' → '%s' (level=%s method=%s)",
                            kw, val, level_attr, res.method)
                return (val, level_attr)
    return None


# ================================================================
# AMBIGUOUS DETECTION
# ================================================================

def _check_ambiguous(schools: list, wilayah_level: str,
                    min_distinct: int = 3,
                    use_province_check: bool = False) -> bool:
    if not schools:
        return False

    if wilayah_level == "desa_kelurahan":
        # Cek berdasarkan provinsi jika diminta (untuk nama desa multi-kata
        # yang tersebar luas — threshold lebih rendah: 2 provinsi)
        if use_province_check:
            prov_vals: Set[str] = set()
            for s in schools:
                prov = (s.get("provinsi") or "").lower().strip()
                if prov:
                    prov_vals.add(prov)
            if len(prov_vals) >= 2:
                return True

        # Cek standar berdasarkan kecamatan+kabupaten
        parent_vals: Set[str] = set()
        for s in schools:
            kec = (s.get("kecamatan") or "").lower().strip()
            kab = (s.get("kabupaten") or "").lower().strip()
            if kec and kab:
                parent_vals.add(f"{kec}|{kab}")
        return len(parent_vals) >= min_distinct

    elif wilayah_level == "kecamatan":
        parent_vals = set()
        for s in schools:
            kab = (s.get("kabupaten") or "").lower().strip()
            if kab:
                parent_vals.add(kab)
        return len(parent_vals) >= min_distinct

    elif wilayah_level in ("kabupaten", "provinsi"):
        parent_vals = set()
        for s in schools:
            prov = (s.get("provinsi") or "").lower().strip()
            if prov:
                parent_vals.add(prov)
        return len(parent_vals) >= min_distinct

    return False

def _extract_main_keyword(query: str) -> str:
    stopwords = {
        "daftar", "sekolah", "di", "ada", "cari", "list", "semua",
        "data", "tentang", "sebuah",
        "smp", "sma", "sd", "smk", "tk", "mi", "mts", "ma",
        "man", "mtsn", "min", "negeri", "swasta",
        "desa", "kelurahan", "kecamatan", "kec", "kabupaten", "kab",
        "kota", "provinsi", "prov",
    }
    tokens = re.sub(r"[^\w\s]", " ", query.lower()).split()
    keywords = [t for t in tokens if t not in stopwords and len(t) >= 3]
    return " ".join(keywords).strip()

def _inject_intent_filters(parsed, intent):
    _if = extract_filters_from_intent(intent)

    if _if.get("bentuk_pendidikan") and not parsed.get("bentuk"):
        parsed["bentuk"] = _if["bentuk_pendidikan"]

    if _if.get("bentuk_pendidikan_in") and not parsed.get("bentuk_pendidikan_in"):
        parsed["bentuk_pendidikan_in"] = _if["bentuk_pendidikan_in"]

    if _if.get("akreditasi_exact") and not parsed.get("akreditasi"):
        parsed["akreditasi"] = _if["akreditasi_exact"]

    if _if.get("akreditasi_in") and not parsed.get("akreditasi_in"):
        parsed["akreditasi_in"] = _if["akreditasi_in"]

    if _if.get("status_sekolah") and not parsed.get("status"):
        parsed["status"] = _if["status_sekolah"]

    if getattr(intent, 'wilayah_terpencil', None) is not None:
        parsed['wilayah_terpencil'] = intent.wilayah_terpencil
        logger.info("[LIST] wilayah_terpencil filter: %s", intent.wilayah_terpencil)

    if getattr(intent, 'wilayah_perbatasan', None) is not None:
        parsed['wilayah_perbatasan'] = intent.wilayah_perbatasan
        logger.info("[LIST] wilayah_perbatasan filter: %s", intent.wilayah_perbatasan)

    if getattr(intent, 'partisipasi_bos', None) is not None:
        parsed['partisipasi_bos'] = intent.partisipasi_bos
        logger.info("[LIST] partisipasi_bos filter: %s", intent.partisipasi_bos)

    return parsed

def _inject_intent_wilayah(parsed: dict, intent) -> None:
    """
    Inject intent.wilayah into parsed["wilayah"] + parsed["wilayah_level"]
    only when parsed["wilayah"] is still empty. Does NOT override existing value.
    """
    if intent is None or parsed.get("wilayah"):
        return
    w = getattr(intent, "wilayah", None)
    if w is None:
        return
    wilayah_val = None
    wilayah_level = None
    if getattr(w, "desa", None):
        wilayah_val = w.desa.lower().strip()
        wilayah_level = "desa_kelurahan"
    elif getattr(w, "kecamatan", None):
        wilayah_val = w.kecamatan.lower().strip()
        wilayah_level = "kecamatan"
    elif getattr(w, "kabupaten", None):
        wilayah_val = w.kabupaten.lower().strip()
        wilayah_level = "kabupaten"
    elif getattr(w, "provinsi", None):
        wilayah_val = w.provinsi.lower().strip()
        wilayah_level = "provinsi"
    if wilayah_val:
        parsed["wilayah"] = wilayah_val
        parsed["wilayah_level"] = wilayah_level
        logger.info("[LIST] injecting wilayah from intent: %s (level=%s)", wilayah_val, wilayah_level)


def _intent_to_legacy_parsed(filters: dict) -> dict:
    parsed = {}
    if not filters:
        return parsed

    if filters.get("bentuk_pendidikan"):
        parsed["bentuk"] = filters["bentuk_pendidikan"]
    if filters.get("bentuk_pendidikan_in"):
        parsed["bentuk_pendidikan_in"] = filters["bentuk_pendidikan_in"]
    if filters.get("status_sekolah"):
        parsed["status"] = filters["status_sekolah"]

    # Semua varian akreditasi — TAMBAHAN:
    if filters.get("akreditasi_exact"):
        parsed["akreditasi"] = filters["akreditasi_exact"]
    if filters.get("akreditasi_gte"):
        parsed["akreditasi_gte"] = filters["akreditasi_gte"]
    if filters.get("akreditasi_in"):
        parsed["akreditasi_in"] = filters["akreditasi_in"]
    if filters.get("akreditasi_exclude"):
        parsed["akreditasi_exclude"] = filters["akreditasi_exclude"]
    if filters.get("penyelenggara"):
        parsed["penyelenggara"] = filters["penyelenggara"]

    return parsed


class ListSchoolHandler:

    def __init__(self, es: AsyncElasticsearch, index_name: str):
        self.es = es
        self.index = index_name

    async def handle(
    self,
    query: str,
    intent=None,
    filters: Optional[Dict] = None,
    _parsed_override: dict = None,
    _depth: int = 0
    ) -> dict:

        logger.info("[LIST] query='%s'", query)

        # ambil filter dari cognitive router
        filters_dict = _parsed_override or filters or extract_filters_from_intent(intent) or {}

        # convert ke format lama agar compatible dengan handler existing
        parsed = _intent_to_legacy_parsed(filters_dict)

        q_low = query.lower()

        _GEO_KEYWORDS_GUARD = {"terdekat", "dekat", "terdeket", "terkedat", "sekitar"}

        # ── Step 0: Deteksi dua lokasi dipisah "dan/serta" ────────
        dan_result = await self._handle_dan_split(query, intent, filters, _depth)
        if dan_result is not None:
            return dan_result

        # ── Step 1: Multi-wilayah eksplisit ──────────────────────
        multi = _extract_multi_wilayah(query)
        if multi and len(multi) >= 2:
            logger.info("[LIST] Multi-wilayah detected: %s", multi)
            logger.info("[LIST] parsed akreditasi=%s", parsed.get("akreditasi"))
            parsed = _inject_intent_filters(parsed, intent)
            return await self._handle_multi_wilayah(query, parsed, multi, intent=intent)

        # ── Step 1b: Trust intent.wilayah dari CognitiveRouter jika scope jelas ──
        # Jika CognitiveRouter sudah memberikan wilayah dengan scope provinsi/kabupaten/
        # kecamatan yang jelas, langsung inject dan skip multikata detection.
        # Ini mencegah nama provinsi (mis. "Jawa Tengah") salah dideteksi sebagai desa.
        _intent_scope = getattr(intent, "scope", None)
        _intent_wil   = getattr(intent, "wilayah", None)

        # ── Step 1b-nasional: scope nasional → skip wilayah detection, langsung execute ──
        if _intent_scope == "nasional":
            parsed = _inject_intent_filters(parsed, intent)
            return await self._handle_nasional_list(query, parsed, intent)

        trusted_result = await self._handle_trusted_scope(
            query, parsed, intent, _intent_scope, _intent_wil
        )
        if trusted_result is not None:
            return trusted_result

        # ── Step 2: Cek nama multi-kata ───────────────────────────
        has_level_hint = bool(re.search(
            r"\b(desa|kelurahan|kecamatan|kec|kabupaten|kab|"
            r"kota|provinsi|prov)\b", q_low
        ))

        detected_multikata = None

        if not has_level_hint:
            stopwords_q = {
                "daftar", "sekolah", "di", "ada", "cari", "list",
                "smp", "sma", "sd", "smk", "tk", "mi", "mts", "ma",
                "man", "mtsn", "min", "negeri", "swasta",
            }
            q_clean = re.sub(r"[^\w\s]", " ", q_low).strip()
            tokens  = [t for t in q_clean.split() if t not in stopwords_q and len(t) >= 3]

            if len(tokens) >= 2:
                mk_result = _is_multikata_with_context(tokens)
                if mk_result:
                    mk_name_level, mk_extra = mk_result
                    if mk_extra and len(mk_extra) >= 2:
                        logger.info("[LIST] Multi-kata+context → multi-wilayah: %s", mk_extra)
                        parsed = _inject_intent_filters(parsed, intent)
                        return await self._handle_multi_wilayah(query, parsed, mk_extra, intent=intent)
                    else:
                        detected_multikata = mk_name_level
                        logger.info("[LIST] Multi-kata location early: '%s' level=%s",
                                    mk_name_level[0], mk_name_level[1])

        # ── Step 2b: Implicit multi-wilayah ──────────────────────
        if not detected_multikata and not has_level_hint:
            implicit = _detect_implicit_multi_wilayah(query)
            if implicit and len(implicit) >= 2:
                logger.info("[LIST] Implicit multi-wilayah: %s", implicit)
                parsed = _inject_intent_filters(parsed, intent)
                return await self._handle_multi_wilayah(query, parsed, implicit, intent=intent)

        # ── Step 2b2: Reversed token order ───────────────────────
        # "barat jawa" → coba reversed "jawa barat" sebagai multi-kata
        if not detected_multikata and not has_level_hint:
            stopwords_rev = {
                "daftar", "sekolah", "di", "ada", "cari", "list",
                "smp", "sma", "sd", "smk", "tk", "mi", "mts", "ma",
                "man", "mtsn", "min", "negeri", "swasta",
            }
            q_clean_rev = re.sub(r"[^\w\s]", " ", q_low).strip()
            tokens_rev  = [t for t in q_clean_rev.split() if t not in stopwords_rev and len(t) >= 3]
            if len(tokens_rev) >= 2:
                reversed_tokens = list(reversed(tokens_rev))
                mk_rev = _is_multikata_location(reversed_tokens)
                if mk_rev:
                    logger.info("[LIST] Reversed-token multi-kata: '%s' level=%s", mk_rev[0], mk_rev[1])
                    detected_multikata = mk_rev

        # ── Step 2c: Desa hint + multi-kata + kecamatan ──────────
        # Contoh: "desa bukit subur penajam" → has_level_hint=True
        # tapi ada nama desa multi-kata + kecamatan setelahnya
        if has_level_hint and not detected_multikata:
            stopwords_hint = {
                "daftar", "sekolah", "di", "ada", "cari", "list",
                "smp", "sma", "sd", "smk", "tk", "mi", "mts", "ma",
                "man", "mtsn", "min", "negeri", "swasta",
                "desa", "kelurahan", "kecamatan", "kec",
                "kabupaten", "kab", "kota", "provinsi", "prov",
            }
            q_clean2 = re.sub(r"[^\w\s]", " ", q_low).strip()
            tokens2  = [t for t in q_clean2.split() if t not in stopwords_hint and len(t) >= 3]
            if len(tokens2) >= 2:
                mk_result2 = _is_multikata_with_context(tokens2)
                if mk_result2:
                    mk_name_level2, mk_extra2 = mk_result2
                    if mk_extra2 and len(mk_extra2) >= 2:
                        logger.info("[LIST] Hint+multi-kata+context → multi-wilayah: %s", mk_extra2)
                        parsed = _inject_intent_filters(parsed, intent)
                        return await self._handle_multi_wilayah(query, parsed, mk_extra2, intent=intent)

        # ── Step 3: Single wilayah ────────────────────────────────
        if detected_multikata:
            multikata_name, multikata_level = detected_multikata
            parsed["wilayah"] = multikata_name
            wilayah_level     = multikata_level
            if re.search(r"\bprovinsi\b", q_low):
                wilayah_level = "provinsi"
            elif re.search(r"\b(kabupaten|kab)\b", q_low):
                wilayah_level = "kabupaten"
            elif re.search(r"\b(kecamatan|kec)\b", q_low):
                wilayah_level = "kecamatan"
            elif re.search(r"\b(desa|kelurahan)\b", q_low):
                wilayah_level = "desa_kelurahan"
            logger.info("[LIST] Using multi-kata '%s' level=%s", multikata_name, wilayah_level)
            return await self._query_single_wilayah(
                query, parsed, wilayah_level,
                has_level_hint=False,
                is_from_multikata=True,
                intent=intent,
            )

        # Fallback normal
        return await self._handle_no_wilayah_fallback(query, parsed, q_low, has_level_hint, intent)

    async def _handle_dan_split(self, query: str, intent, filters, _depth: int):
        if _depth != 0:
            return None
        dan_parts = _split_dan_query(query)
        if not dan_parts or len(dan_parts) < 2:
            return None
        logger.info("[LIST] Dan-split detected: %s", dan_parts)

        # Untuk setiap sub-query, inject scope=kabupaten agar resolver langsung
        # ke level kab/kota dan tidak jatuh ke multi-level clarification
        # (mis. "bandung" → 24 kandidat kecamatan/desa dari berbagai provinsi).
        def _make_task(part: str):
            loc_kw = _extract_location_keyword(part)
            if loc_kw:
                hint_wil    = WilayahEntities(kabupaten=loc_kw)
                hint_intent = CognitiveIntent(
                    module="list",
                    scope="kabupaten",
                    wilayah=hint_wil,
                    jenjang=getattr(intent, "jenjang", None) if intent else None,
                    jenjang_list=getattr(intent, "jenjang_list", []) if intent else [],
                    status=getattr(intent, "status", None) if intent else None,
                    akreditasi=getattr(intent, "akreditasi", None) if intent else None,
                )
                return self.handle(part, intent=hint_intent, filters=filters, _depth=1)
            return self.handle(part, intent=None, filters=filters, _depth=1)

        raw_results = await asyncio.gather(
            *[_make_task(p) for p in dan_parts],
            return_exceptions=True,
        )
        ok_results = []
        for part, r in zip(dan_parts, raw_results):
            if isinstance(r, Exception):
                logger.warning("[LIST] Dan-split sub-query gagal untuk '%s': %s", part, r)
                continue
            if r.get("status") == "success":
                ok_results.append(r)

        if len(ok_results) < 2:
            return None

        merged_schools = []
        merged_total   = 0
        breakdown      = {}
        for r in ok_results:
            merged_schools += r["schools"]
            merged_total   += r["total"]
            wilayah = r.get("parsed", {}).get("wilayah", "")
            breakdown[wilayah] = r["total"]

        # Buat label wilayah tanpa duplikat
        wilayah_labels = list(dict.fromkeys(
            r.get("parsed", {}).get("wilayah", "") for r in ok_results
        ))
        merged_wilayah_label = " dan ".join(w for w in wilayah_labels if w)

        merged_parsed = dict(ok_results[0].get("parsed", {}))
        merged_parsed["wilayah"]             = merged_wilayah_label
        merged_parsed["dan_split_breakdown"] = breakdown
        merged_parsed["dan_split_total"]     = merged_total
        logger.info("[LIST] Dan-split merged: %s = %d schools",
                    " + ".join(str(r["total"]) for r in ok_results), merged_total)
        return {
            "status": "success", "intent": "school_list",
            "total": merged_total, "schools": merged_schools,
            "parsed": merged_parsed,
            "wilayah_level": ok_results[0].get("wilayah_level", "desa_kelurahan"),
            "is_ambiguous": False, "candidates": [],
            "breakdown_jenjang": ok_results[0].get("breakdown_jenjang"),
            "breakdown_status":  ok_results[0].get("breakdown_status"),
        }

    async def _handle_nasional_list(self, query: str, parsed: dict, intent) -> dict:
        logger.info("[LIST] Trusted intent scope=nasional → no wilayah filter, QC only")
        qc_filters_nas = build_qc_filter(intent)

        es_filters = []
        bentuk_in  = parsed.get("bentuk_pendidikan_in")
        bentuk     = parsed.get("bentuk")
        status     = parsed.get("status")
        akred      = parsed.get("akreditasi")
        akred_in   = parsed.get("akreditasi_in")  # TT / multi-value

        if bentuk_in:
            es_filters.append({"terms": {"bentuk_pendidikan": bentuk_in}})
        elif bentuk:
            es_filters.append({"term": {"bentuk_pendidikan": bentuk}})
        if status:
            es_filters.append({"term": {"status_sekolah": status.upper()}})
        if akred_in:
            es_filters.append({"terms": {"akreditasi": akred_in}})
        elif akred:
            from app.services.core.aggregation_core import _AKRED_ES_ALIAS
            akred_mapped = _AKRED_ES_ALIAS.get(akred.upper(), akred)
            es_filters.append({"term": {"akreditasi": akred_mapped}})
        es_filters.extend(qc_filters_nas)

        sort_mode  = _detect_sort_mode(query, intent=intent)
        query_body = {
            "size": 10,
            "track_total_hits": True,
            "query": {
                "bool": {
                    "filter": es_filters if es_filters else [{"match_all": {}}],
                }
            },
            "collapse": {"field": "npsn"},
            "sort": _build_sort_clause(sort_mode, intent=intent),
        }
        query_body.update(LIST_AGGS_SNIPPET)
        qvec = None
        try:
            qvec = await get_cached_embedding(query)
        except Exception:
            pass
        logger.info("[LIST] Nasional hybrid: qvec_len=%d", len(qvec) if qvec else 0)
        if qvec:
            _hybridize_query(query_body, qvec)

        result    = await self.es.search(index=self.index, body=query_body, request_timeout=5)
        hits      = result["hits"]["hits"]
        total_val = result["hits"]["total"]
        total     = total_val["value"] if isinstance(total_val, dict) else total_val
        schools   = [self._format_hit(h) for h in hits]
        bdj, bds  = extract_breakdowns(result)

        qc_masalah_flag = getattr(intent, "qc_masalah_only", False)
        return {
            "status": "success",
            "intent": "school_list",
            "total": total,
            "schools": schools,
            "parsed": {
                **parsed,
                "wilayah": "nasional",
                **({"qc_masalah_only": True} if qc_masalah_flag else {}),
            },
            "wilayah_level": "nasional",
            "is_ambiguous": False,
            "candidates": [],
            "breakdown_jenjang": bdj,
            "breakdown_status": bds,
        }

    async def _handle_trusted_scope(self, query: str, parsed: dict, intent, _intent_scope, _intent_wil):
        _TRUSTED_SCOPES = {"provinsi", "kabupaten", "kecamatan", "desa"}
        if (
            _intent_scope not in _TRUSTED_SCOPES
            or _intent_wil is None
            or parsed.get("wilayah")
        ):
            return None
        wil_val   = None
        wil_level = None
        if _intent_scope == "provinsi" and getattr(_intent_wil, "provinsi", None):
            wil_val   = _intent_wil.provinsi.lower().strip()
            wil_level = "provinsi"
        elif _intent_scope == "kabupaten" and getattr(_intent_wil, "kabupaten", None):
            wil_val   = _intent_wil.kabupaten.lower().strip()
            wil_level = "kabupaten"
            # Province alias check: bare name like "jakarta"/"yogyakarta" → upgrade to province
            _has_kab_prefix = any(wil_val.startswith(p)
                                  for p in ("kab.", "kota ", "kota adm.", "kabupaten "))
            if not _has_kab_prefix:
                _prov_alias = PROVINCE_ALIAS_MAP.get(wil_val)
                if _prov_alias:
                    wil_val   = _prov_alias
                    wil_level = "provinsi"
                    logger.info("[LIST] Province alias upgrade: '%s' → provinsi='%s'",
                                _intent_wil.kabupaten, wil_val)
        elif _intent_scope == "kecamatan" and getattr(_intent_wil, "kecamatan", None):
            wil_val   = _intent_wil.kecamatan.lower().strip()
            wil_level = "kecamatan"
        elif _intent_scope == "desa" and getattr(_intent_wil, "desa", None):
            wil_val   = _intent_wil.desa.lower().strip()
            wil_level = "desa_kelurahan"

        if wil_level == "kabupaten" and wil_val and not wil_val.startswith("kota "):
            q_lower = query.lower()
            if (re.search(r"\bkota\b", q_lower)
                    and not re.search(r"\b(kabupaten|kab\.?)\b", q_lower)):
                wil_val = f"kota {wil_val}"
                logger.info(
                    "[LIST] Override want_kota=True dari query original: '%s' → wilayah='%s'",
                    query[:60], wil_val,
                )

        if not (wil_val and wil_level):
            return None
        parsed["wilayah"]       = wil_val
        parsed["wilayah_level"] = wil_level
        parsed = _inject_intent_filters(parsed, intent)
        logger.info(
            "[LIST] Trusted intent scope=%s → wilayah='%s' level=%s, skip multikata",
            _intent_scope, wil_val, wil_level,
        )
        return await self._query_single_wilayah(
            query, parsed, wil_level,
            has_level_hint=True,
            is_from_multikata=True,
            intent=intent,
        )

    async def _handle_no_wilayah_fallback(
        self, query: str, parsed: dict, q_low: str, has_level_hint: bool, intent
    ) -> dict:
        wilayah_level = "kabupaten"
        if not parsed.get("wilayah"):
            _inject_intent_wilayah(parsed, intent)
        if not parsed.get("wilayah"):
            result = _fallback_detect_wilayah(query)
            logger.debug(
                "[LIST] fallback result=%s parsed_before_inject=%s intent_jenjang=%s",
                result, parsed, getattr(intent, 'jenjang', None)
            )
            if result:
                wilayah_name, wilayah_level = result
                parsed["wilayah"] = wilayah_name
                parsed = _inject_intent_filters(parsed, intent)
        else:
            if parsed.get("wilayah_level"):
                wilayah_level = parsed["wilayah_level"]
            elif re.search(r"\b(desa|kelurahan)\b", q_low):
                wilayah_level = "desa_kelurahan"
            elif re.search(r"\bprovinsi\b", q_low):
                wilayah_level = "provinsi"
            elif re.search(r"\b(kecamatan|kec)\b", q_low):
                wilayah_level = "kecamatan"
            else:
                wilayah_level = "kabupaten"

        if not parsed.get("wilayah"):
            kw = _extract_location_keyword(query)

            # Pulau alias: "pulau jawa", "sulawesi", etc. → multi-province terms filter
            pulau_result = _resolve_pulau_keyword(kw) if kw else None
            if pulau_result:
                pulau_name, provinsi_list = pulau_result
                parsed["wilayah"] = kw.lower().strip()
                parsed["provinsi_list"] = provinsi_list
                parsed = _inject_intent_filters(parsed, intent)
                logger.info("[LIST] Pulau alias: '%s' → %d provinsi", pulau_name, len(provinsi_list))
                return await self._query_single_wilayah(
                    query, parsed, "provinsi",
                    has_level_hint=True, is_from_multikata=True, intent=intent,
                )

            candidates = _find_location_candidates(kw) if kw else []

            if not candidates and kw and len(kw) >= 4:
                kw_main = _extract_main_keyword(query)
                _level_only_words = {"kecamatan", "kabupaten", "provinsi", "desa", "kelurahan", "kota"}
                kw_main_tokens = set((kw_main or "").lower().split())
                if kw_main_tokens and kw_main_tokens.issubset(_level_only_words):
                    kw_main = ""
                fuzzy = _apply_typo_correction(kw_main or kw, q_low) if kw_main else None
                if fuzzy:
                    corrected_name, corrected_level = fuzzy
                    logger.info("[LIST] Typo corrected (no-wilayah): '%s' → '%s'", kw, corrected_name)
                    parsed["wilayah"] = corrected_name
                    if re.search(r"\b(desa|kelurahan)\b", q_low):
                        wilayah_level = "desa_kelurahan"
                    elif re.search(r"\bprovinsi\b", q_low):
                        wilayah_level = "provinsi"
                    elif re.search(r"\b(kecamatan|kec)\b", q_low):
                        wilayah_level = "kecamatan"
                    elif re.search(r"\b(kabupaten|kab)\b", q_low):
                        wilayah_level = "kabupaten"
                    elif re.search(r"\bkota\b", q_low):
                        wilayah_level = "kabupaten"
                    else:
                        wilayah_level = corrected_level
                    return await self._query_single_wilayah(
                        query, parsed, wilayah_level, has_level_hint,
                        original_kw=kw, corrected_kw=corrected_name,
                        intent=intent,
                    )

            if len(candidates) == 1:
                single = candidates[0]
                parsed["wilayah"] = single["nama"]
                wlevel = single["level"]
                logger.info("[LIST] Single candidate auto-resolve: '%s' level=%s",
                            single["nama"], wlevel)
                return await self._query_single_wilayah(
                    query, parsed, wlevel,
                    has_level_hint=True,
                    is_from_multikata=True,
                    intent=intent,
                )

            logger.info("[LIST] No wilayah detected, keyword=%s candidates=%d", kw, len(candidates))
            return {
                "status": "clarification", "intent": "school_list",
                "total": 0, "schools": [], "parsed": parsed,
                "wilayah_level": wilayah_level, "is_ambiguous": True,
                "keyword": kw, "candidates": candidates,
            }

        return await self._query_single_wilayah(query, parsed, wilayah_level, has_level_hint, intent=intent)

    def _resolve_province_siblings(
        self, parsed: dict, wilayah_kw: str, wilayah_level: str
    ) -> Optional[dict]:
        """
        Cek apakah wilayah_kw adalah nama provinsi yang punya sibling match.
        Return clarification dict jika perlu disambiguasi, None jika bisa lanjut.
        Mutates parsed["wilayah"] saat auto-resolve (dominant prov).
        """
        if not _has_province_siblings(wilayah_kw):
            return None
        candidates = _find_location_candidates(wilayah_kw)
        prov_cands = [c for c in candidates if c["level"] == "provinsi"]
        non_prov   = [c for c in candidates if c["level"] != "provinsi"]
        if (len(prov_cands) == 1
                and all(c["level"] == "desa_kelurahan" for c in non_prov)):
            parsed["wilayah"] = prov_cands[0]["nama"]
            logger.info("[LIST] Siblings override: dominant prov '%s'", prov_cands[0]["nama"])
            return None
        logger.info("[LIST] Province siblings found for '%s', clarification", wilayah_kw)
        return {
            "status": "clarification", "intent": "school_list",
            "total": 0, "schools": [], "parsed": parsed,
            "wilayah_level": wilayah_level, "is_ambiguous": True,
            "keyword": wilayah_kw, "candidates": candidates,
        }

    def _resolve_dominant_location(
        self, candidates: list, wilayah_level: str, intent, wilayah_kw: str
    ) -> Tuple[Optional[dict], str, str]:
        """
        Resolve wilayah dari daftar kandidat ke satu pilihan.
        Return: (clarification_dict_or_None, new_wilayah_value, new_wilayah_level)
        Caller mengupdate parsed["wilayah"] dan wilayah_level jika new_wilayah tidak kosong.
        """
        if len(candidates) == 1:
            single = candidates[0]
            logger.info("[LIST] Single exact candidate '%s' level=%s, skip clarification",
                        single["nama"], single["level"])
            return None, single["nama"], single["level"]

        kab_candidates  = [c for c in candidates if c["level"] == "kabupaten"]
        prov_candidates = [c for c in candidates if c["level"] == "provinsi"]

        if len(kab_candidates) == 1:
            kab = kab_candidates[0]
            logger.info(
                "[LIST] Dominant kab auto-resolve: '%s' (%d sub-candidates ignored)",
                kab["nama"], len(candidates) - 1,
            )
            return None, kab["nama"], "kabupaten"

        if len(prov_candidates) == 1 and all(
            c["level"] == "desa_kelurahan" for c in candidates if c["level"] != "provinsi"
        ):
            prov = prov_candidates[0]
            logger.info(
                "[LIST] Dominant prov auto-resolve: '%s' (%d desa-only sub-candidates ignored)",
                prov["nama"], len(candidates) - 1,
            )
            return None, prov["nama"], "provinsi"

        # Intent guard + same-location check
        _intent_wil = getattr(intent, "wilayah", None) if intent else None
        _intent_kab  = getattr(_intent_wil, "kabupaten", None) if _intent_wil else None
        _intent_prov = getattr(_intent_wil, "provinsi", None) if _intent_wil else None

        if _intent_kab and _intent_kab.lower().strip() == wilayah_kw:
            logger.info("[LIST] same-location guard: intent.kabupaten='%s' matches wilayah_kw, skip", _intent_kab)
            return None, "", ""  # no change, lanjut ke _build_query

        if _intent_prov and _intent_prov.lower().strip() in wilayah_kw:
            prov_val = _intent_prov.lower().strip()
            logger.info("[LIST] same-location guard: intent.provinsi='%s', override ke provinsi", _intent_prov)
            return None, prov_val, "provinsi"

        kab_vals = set(c.get("kabupaten", "").lower() for c in candidates if c.get("kabupaten"))
        kec_vals = set(c.get("kecamatan", "").lower() for c in candidates if c.get("kecamatan"))
        if len(kab_vals) == 1 and len(kec_vals) == 1:
            kec_clean = re.sub(r"^kec\.\s*", "", list(kec_vals)[0]).strip()
            logger.info("[LIST] Same-location auto-resolve: all candidates → kec '%s'", kec_clean)
            return None, kec_clean, "kecamatan"

        logger.info("[LIST] Multi-level clarification: wilayah=%s candidates=%d", wilayah_kw, len(candidates))
        return {
            "status": "clarification", "intent": "school_list",
            "total": 0, "schools": [], "parsed": {},
            "wilayah_level": wilayah_level, "is_ambiguous": True,
            "keyword": wilayah_kw, "candidates": candidates,
        }, "", ""

    async def _apply_typo_and_retry(
        self, query: str, parsed: dict, wilayah_kw: str,
        wilayah_level: str, has_level_hint: bool, intent,
    ) -> Optional[dict]:
        """
        Jika total == 0, coba koreksi typo wilayah dan retry _query_single_wilayah.
        Return hasil dict baru jika retry berhasil, None jika tidak ada kandidat typo.
        """
        kw_main = _extract_main_keyword(query)
        fuzzy   = _apply_typo_correction(kw_main or wilayah_kw, query.lower())
        if not fuzzy or fuzzy[0] == wilayah_kw:
            return None
        corrected_name, corrected_level = fuzzy
        logger.info("[LIST] Typo after 0 results: '%s' → '%s'", wilayah_kw, corrected_name)
        parsed2 = dict(parsed)
        parsed2["wilayah"] = corrected_name
        q_low = query.lower()
        if re.search(r"\bprovinsi\b", q_low):
            new_level = "provinsi"
        elif re.search(r"\b(kabupaten|kab)\b", q_low):
            new_level = "kabupaten"
        elif re.search(r"\bkota\b", q_low):
            new_level = "kabupaten"
        elif re.search(r"\b(kecamatan|kec)\b", q_low):
            new_level = "kecamatan"
        elif re.search(r"\b(desa|kelurahan)\b", q_low):
            new_level = "desa_kelurahan"
        else:
            new_level = corrected_level
        return await self._query_single_wilayah(
            query, parsed2, new_level, has_level_hint,
            original_kw=wilayah_kw, corrected_kw=corrected_name,
            intent=intent,
        )

    def _build_scattered_result(
        self, schools: list, wilayah_kw: str, wilayah_level: str,
        total: int, parsed: dict, has_level_hint: bool,
    ) -> Optional[dict]:
        """
        Cek apakah hasil ES tersebar di banyak provinsi/kab (ambiguous).
        Return clarification dict jika scattered, None jika aman.
        """
        is_desa_level = (wilayah_level == "desa_kelurahan" and bool(wilayah_kw))
        if not has_level_hint or is_desa_level:
            if _check_ambiguous(schools, wilayah_level, min_distinct=3, use_province_check=is_desa_level):
                kw         = wilayah_kw or ""
                candidates = _find_location_candidates(kw) if kw else []
                logger.info("[LIST] Scattered: wilayah=%s candidates=%d", kw, len(candidates))
                return {
                    "status": "clarification", "intent": "school_list",
                    "total": total, "schools": [], "parsed": parsed,
                    "wilayah_level": wilayah_level, "is_ambiguous": True,
                    "keyword": kw, "candidates": candidates,
                }
        return None

    def _apply_qc_flags(self, parsed: dict, intent) -> dict:
        """Inject flag qc_masalah_only / qc_field ke parsed jika intent menandainya."""
        qc_masalah_only = getattr(intent, "qc_masalah_only", False) if intent else False
        qc_field        = getattr(intent, "qc_field", None) if intent else None
        qc_field_list   = getattr(intent, "qc_field_list", []) if intent else []
        if qc_masalah_only:
            parsed = {**parsed, "qc_masalah_only": True}
            if qc_field:
                parsed["qc_field"] = qc_field
        if qc_field_list:
            parsed["qc_field_list"] = qc_field_list
        return parsed

    def _apply_filter_from_intent(self, parsed: dict, intent) -> None:
        """Inject filter bentuk/akreditasi/status/numeric_filters dari intent ke parsed jika belum ada."""
        _if = extract_filters_from_intent(intent)
        if _if.get("bentuk_pendidikan") and not parsed.get("bentuk"):
            parsed["bentuk"] = _if["bentuk_pendidikan"]
        if _if.get("bentuk_pendidikan_in") and not parsed.get("bentuk_pendidikan_in"):
            parsed["bentuk_pendidikan_in"] = _if["bentuk_pendidikan_in"]
        if _if.get("akreditasi_exact") and not parsed.get("akreditasi"):
            parsed["akreditasi"] = _if["akreditasi_exact"]
        if _if.get("akreditasi_in") and not parsed.get("akreditasi_in"):
            parsed["akreditasi_in"] = _if["akreditasi_in"]
        if _if.get("status_sekolah") and not parsed.get("status"):
            parsed["status"] = _if["status_sekolah"]
        numeric_filters = getattr(intent, "numeric_filters", None) or []
        if numeric_filters and not parsed.get("numeric_filters"):
            parsed["numeric_filters"] = numeric_filters
            logger.info("[LIST] numeric_filters injected: %s",
                        [(f.field, f.operator, f.value) for f in numeric_filters])

    async def _query_single_wilayah(
        self, query: str, parsed: dict, wilayah_level: str,
        has_level_hint: bool, intent,
        original_kw: str = "", corrected_kw: str = "",
        is_from_multikata: bool = False,
    ) -> dict:

        wilayah_kw = parsed.get("wilayah", "")

        # Provinsi dengan sibling
        if not has_level_hint and not is_from_multikata and wilayah_level == "provinsi" and wilayah_kw:
            result = self._resolve_province_siblings(parsed, wilayah_kw, wilayah_level)
            if result is not None:
                return result

        # Multi-level match
        if not has_level_hint and not is_from_multikata and wilayah_kw and _has_multi_level_match(wilayah_kw):
            candidates = _find_location_candidates(wilayah_kw)
            clarification, new_wilayah, new_level = self._resolve_dominant_location(
                candidates, wilayah_level, intent, wilayah_kw)
            if isinstance(clarification, dict):
                clarification["parsed"] = parsed
                return clarification
            if new_wilayah:
                parsed["wilayah"] = new_wilayah
                wilayah_level = new_level

        self._apply_filter_from_intent(parsed, intent)

        _qvec = None
        try:
            _qvec = await get_cached_embedding(query)
        except Exception:
            pass
        logger.info("[LIST] Single-wilayah hybrid: qvec_len=%d", len(_qvec) if _qvec else 0)
        es_query = self._build_query(parsed, wilayah_level, query_raw=query, intent=intent)
        # Tambahkan aggregation breakdown
        es_query.update(LIST_AGGS_SNIPPET)
        if _qvec:
            _hybridize_query(es_query, _qvec)
        result   = await self.es.search(index=self.index, body=es_query, request_timeout=5)

        hits      = result["hits"]["hits"]
        total_val = result["hits"]["total"]
        total     = total_val["value"] if isinstance(total_val, dict) else total_val
        schools   = [self._format_hit(h) for h in hits]
        # ── Deteksi sort mode dari query ─────────────────────────────────
        _sort_mode = _detect_sort_mode(query, intent=intent)
        # Extract aggregation breakdown
        breakdown_jenjang, breakdown_status = extract_breakdowns(result)
        # Typo correction jika 0 hasil
        if total == 0 and wilayah_kw and not corrected_kw:
            retry = await self._apply_typo_and_retry(
                query, parsed, wilayah_kw, wilayah_level, has_level_hint, intent)
            if retry is not None:
                return retry

        scattered = self._build_scattered_result(
            schools, wilayah_kw, wilayah_level, total, parsed, has_level_hint)
        if scattered is not None:
            return scattered

        parsed = self._apply_qc_flags(parsed, intent)

        response = {
            "status": "success", "intent": "school_list",
            "total": total,
            "schools": schools,
            "parsed": parsed,
            "wilayah_level": wilayah_level,
            "is_ambiguous": False,
            "candidates": [],
            "breakdown_jenjang": breakdown_jenjang,
            "breakdown_status": breakdown_status,
        }
        if corrected_kw:
            response["typo_corrected"] = {"original": original_kw, "corrected": corrected_kw}
        return response

    async def _handle_multi_wilayah(self, query: str, parsed: Dict, multi: Dict, intent=None) -> Dict:
        desa      = multi.get("desa") or multi.get("kelurahan") or ""
        kecamatan = multi.get("kecamatan") or ""
        kabupaten = multi.get("kabupaten") or ""
        kota      = multi.get("kota") or ""
        provinsi  = multi.get("provinsi") or ""

        want_kota = bool(kota and not kabupaten)
        want_kab  = bool(kabupaten and not kota)
        kab_name  = kabupaten or kota

        filters = []
        should  = []

        bentuk      = parsed.get("bentuk")
        bentuk_in   = parsed.get("bentuk_pendidikan_in")  # PAUD expand
        akreditasi  = parsed.get("akreditasi")
        akred_in    = parsed.get("akreditasi_in")         # TT / multi-value
        status      = parsed.get("status")

        if not status:
            sn = (parsed.get("school_name") or "").lower()
            if re.search(r"\bnegeri\b", sn):
                status = "NEGERI"
        if bentuk_in:
            filters.append({"terms": {"bentuk_pendidikan": bentuk_in}})
        elif bentuk == "PAUD":
            filters.append({"terms": {"bentuk_pendidikan": _PAUD_BENTUK_EXPAND}})
        elif bentuk:
            filters.append({"term": {"bentuk_pendidikan": bentuk}})
        if status:
            filters.append({"term": {"status_sekolah": status.upper()}})
        if akred_in:
            filters.append({"terms": {"akreditasi": akred_in}})
            logger.info("[LIST] Multi: filter akreditasi_in=%s", akred_in)
        elif akreditasi:
            from app.services.core.aggregation_core import _AKRED_ES_ALIAS
            akred_mapped = _AKRED_ES_ALIAS.get(akreditasi.upper(), akreditasi)
            filters.append({"term": {"akreditasi": akred_mapped}})
            logger.info("[LIST] Multi: filter akreditasi=%s → ES=%s", akreditasi, akred_mapped)

        if desa:
            filters.append({"bool": {"should": [
                {"term":  {"desa_kelurahan": desa}},
                match_phrase("desa_kelurahan.text", desa),
            ], "minimum_should_match": 1}})
            should.append({"term": {"desa_kelurahan": {"value": desa, "boost": 30}}})
            logger.info("[LIST] Multi: filter desa=%s", desa)

        if kecamatan:
            filters.append({"bool": {"should": [
                {"term":  {"kecamatan": kecamatan}},
                {"term":  {"kecamatan": f"kec. {kecamatan}"}},
                match_phrase("kecamatan.text", kecamatan),
            ], "minimum_should_match": 1}})
            should.append({"term": {"kecamatan": {"value": kecamatan, "boost": 25}}})
            should.append({"term": {"kecamatan": {"value": f"kec. {kecamatan}", "boost": 25}}})
            logger.info("[LIST] Multi: filter kecamatan=%s", kecamatan)

        if kab_name:
            if want_kota:
                filters.append({"bool": {"should": [
                    {"term": {"kabupaten": f"kota {kab_name}"}},
                    {"term": {"kabupaten": f"kab. kota {kab_name}"}},
                    match_phrase("kabupaten.text", kab_name),
                ], "minimum_should_match": 1}})
                should.append({"term": {"kabupaten": {"value": f"kota {kab_name}", "boost": 25}}})
            elif want_kab:
                filters.append({"bool": {"should": [
                    {"term": {"kabupaten": kab_name}},
                    {"term": {"kabupaten": f"kab. {kab_name}"}},
                ], "minimum_should_match": 1}})
                should.append({"term": {"kabupaten": {"value": f"kab. {kab_name}", "boost": 25}}})
            else:
                filters.append({"bool": {"should": [
                    {"term": {"kabupaten": kab_name}},
                    {"term": {"kabupaten": f"kab. {kab_name}"}},
                    {"term": {"kabupaten": f"kota {kab_name}"}},
                    match_phrase("kabupaten.text", kab_name),
                ], "minimum_should_match": 1}})
                should.append({"term": {"kabupaten": {"value": f"kab. {kab_name}", "boost": 20}}})
                should.append({"term": {"kabupaten": {"value": f"kota {kab_name}", "boost": 20}}})
            logger.info("[LIST] Multi: filter kab=%s", kab_name)

        if provinsi:
            filters.append({"bool": {"should": [
                {"match": {"provinsi.text": provinsi}},
                {"term":  {"provinsi": provinsi}},
                {"term":  {"provinsi": f"prov. {provinsi}"}},
            ], "minimum_should_match": 1}})

        if query:
            keyword = _extract_main_keyword(query)
            if keyword:
                should.append({
                    "multi_match": {
                        "query": keyword,
                        "fields": ["nama^3", "nama.autocomplete", "penyelenggara^2"],
                        "type": "best_fields",
                        "fuzziness": "AUTO",
                        "boost": 5
                    }
                })

        # ── QC Filter — inject dari intent jika ada ──────────────────────────────
        if intent is not None:
            qc_filters = build_qc_filter(intent)
            if qc_filters:
                filters.extend(qc_filters)
                logger.info("[LIST] Multi-wilayah QC filters injected: %s", qc_filters)

        query_body = {
            "size": 10, "track_total_hits": True,
            "query": {"bool": {"filter": filters, "should": should,
                               **({"minimum_should_match": 1} if should else {})}},
            "collapse": {"field": "npsn"},
            "sort": _build_sort_clause(_detect_sort_mode(query, intent=intent), intent=intent),
        }

        _qvec_mw = None
        try:
            _qvec_mw = await get_cached_embedding(query)
        except Exception:
            pass
        logger.info("[LIST] Multi-wilayah hybrid: qvec_len=%d", len(_qvec_mw) if _qvec_mw else 0)
        if _qvec_mw:
            _hybridize_query(query_body, _qvec_mw)
        logger.info("[LIST] Multi-wilayah ES QUERY: %s", query_body)
        result    = await self.es.search(index=self.index, body=query_body, request_timeout=5)
        hits      = result["hits"]["hits"]
        total_val = result["hits"]["total"]
        total     = total_val["value"] if isinstance(total_val, dict) else total_val
        schools   = [self._format_hit(h) for h in hits]

        # ── FALLBACK: desa+kecamatan gabungan ────────────────────
        # Jika query "X di kecamatan Y" menghasilkan 0, coba
        # gabungan desa sebagai satu nama: "X Y" atau "Y X"
        # Contoh: desa=karet, kec=kuningan → coba desa="karet kuningan"
        # Contoh: desa=setia budi, kec=kuningan → cari desa "setia budi" di kec kuningan
        if total == 0 and desa and kecamatan and not kabupaten and not provinsi:
            combined_names = [
                f"{desa} {kecamatan}",   # "karet kuningan"
                f"{kecamatan} {desa}",   # "kuningan karet"
            ]
            # Helper: bentuk filter clause (support PAUD expand)
            def _bentuk_filter():
                if bentuk_in:
                    return [{"terms": {"bentuk_pendidikan": bentuk_in}}]
                elif bentuk == "PAUD":
                    return [{"terms": {"bentuk_pendidikan": _PAUD_BENTUK_EXPAND}}]
                elif bentuk:
                    return [{"term": {"bentuk_pendidikan": bentuk}}]
                return []

            for combined in combined_names:
                fallback_body = {
                    "size": 10, "track_total_hits": True,
                    "query": {"bool": {
                        "filter": _bentuk_filter() + (
                            [{"term": {"status_sekolah": status.upper()}}] if status else []
                        ) + [{
                            "bool": {"should": [
                                {"term":  {"desa_kelurahan": combined}},
                                match_phrase("desa_kelurahan.text", combined),
                            ], "minimum_should_match": 1}
                        }],
                        "should": [
                            {"term": {"desa_kelurahan": {"value": combined, "boost": 30}}},
                            match_phrase("desa_kelurahan.text", combined),
                        ],
                        "minimum_should_match": 1,
                    }},
                    "collapse": {"field": "npsn"},
                    "sort": _build_sort_clause(_detect_sort_mode(query, intent=intent), intent=intent),
                }
                fb_result = await self.es.search(index=self.index, body=fallback_body, request_timeout=5)
                fb_total_val = fb_result["hits"]["total"]
                fb_total = fb_total_val["value"] if isinstance(fb_total_val, dict) else fb_total_val
                if fb_total > 0:
                    logger.info("[LIST] Multi fallback combined desa '%s' → %d results", combined, fb_total)
                    schools = [self._format_hit(h) for h in fb_result["hits"]["hits"]]
                    total   = fb_total
                    display_parts = [combined]
                    parsed_display = dict(parsed)
                    parsed_display["wilayah"] = combined
                    return {
                        "status": "success", "intent": "school_list",
                        "total": total, "schools": schools, "parsed": parsed_display,
                        "wilayah_level": "desa_kelurahan",
                        "is_ambiguous": False, "candidates": [],
                    }

        # ── FALLBACK 2: desa saja (drop kecamatan) ───────────────
        # Jika desa+kecamatan AND combined-name semuanya 0,
        # coba query desa saja — mungkin "kecamatan" di query adalah
        # nama area populer, bukan kecamatan administratif.
        # Contoh: "kuningan karet" → desa=karet (di kec. setia budi Jakarta)
        if total == 0 and desa and kecamatan and not kabupaten and not provinsi:
            desa_only_body = {
                "size": 10, "track_total_hits": True,
                "query": {"bool": {
                    "filter": (
                        [{"terms": {"bentuk_pendidikan": bentuk_in}}] if bentuk_in
                        else ([{"terms": {"bentuk_pendidikan": _PAUD_BENTUK_EXPAND}}] if bentuk == "PAUD"
                        else ([{"term": {"bentuk_pendidikan": bentuk}}] if bentuk else []))
                    ) + (
                        [{"term": {"status_sekolah": status.upper()}}] if status else []
                    ) + [{
                        "bool": {"should": [
                            {"term":  {"desa_kelurahan": desa}},
                            match_phrase("desa_kelurahan.text", desa),
                        ], "minimum_should_match": 1}
                    }],
                    "should": [
                        {"term": {"desa_kelurahan": {"value": desa, "boost": 30}}},
                    ],
                    "minimum_should_match": 1,
                }},
                "collapse": {"field": "npsn"},
                "sort": _build_sort_clause(_detect_sort_mode(query, intent=intent), intent=intent),
            }
            d2_result = await self.es.search(index=self.index, body=desa_only_body, request_timeout=5)
            d2_total_val = d2_result["hits"]["total"]
            d2_total = d2_total_val["value"] if isinstance(d2_total_val, dict) else d2_total_val
            if d2_total > 0:
                logger.info("[LIST] Multi fallback desa-only '%s' → %d results", desa, d2_total)
                # Cek apakah hasil tersebar di banyak lokasi → clarification
                d2_schools = [self._format_hit(h) for h in d2_result["hits"]["hits"]]
                is_scattered = _check_ambiguous(d2_schools, "desa_kelurahan",
                                                min_distinct=3, use_province_check=True)
                if is_scattered:
                    candidates = _find_location_candidates(desa)
                    logger.info("[LIST] Desa-only fallback scattered: %s candidates=%d", desa, len(candidates))
                    return {
                        "status": "clarification", "intent": "school_list",
                        "total": d2_total, "schools": [], "parsed": parsed,
                        "wilayah_level": "desa_kelurahan", "is_ambiguous": True,
                        "keyword": desa, "candidates": candidates,
                    }
                parsed_display2 = dict(parsed)
                parsed_display2["wilayah"] = desa
                return {
                    "status": "success", "intent": "school_list",
                    "total": d2_total, "schools": d2_schools, "parsed": parsed_display2,
                    "wilayah_level": "desa_kelurahan",
                    "is_ambiguous": False, "candidates": [],
                }


        display_parts  = [p for p in [desa, kecamatan, kab_name, provinsi] if p]
        parsed_display = dict(parsed)
        parsed_display["wilayah"] = " ".join(display_parts)

        _qc_flag = getattr(intent, "qc_masalah_only", False) if intent else False
        if _qc_flag:
            parsed_display = {**parsed_display, "qc_masalah_only": True}

        return {
            "status": "success", "intent": "school_list",
            "total": total, "schools": schools, "parsed": parsed_display,
            "wilayah_level": "desa_kelurahan" if desa else "kecamatan",
            "is_ambiguous": False, "candidates": [],
        }

    def _build_query(self, parsed: Dict, wilayah_level: str = "kabupaten",
                    query_raw: str = "", intent=None) -> Dict:
        return build_list_query(parsed, wilayah_level, query_raw, intent)

    def _format_hit(self, hit: Dict) -> Dict:
        src = hit["_source"]
        qc_score = src.get("qc_score") or {}
        result = {
            "nama":           src.get("nama"),
            "npsn":           src.get("npsn"),
            "bentuk":         src.get("bentuk_pendidikan"),
            "status":         src.get("status_sekolah"),
            "akreditasi":     src.get("akreditasi"),
            "siswa_total":    src.get("siswa_total"),
            "desa_kelurahan": src.get("desa_kelurahan"),
            "kecamatan":      src.get("kecamatan"),
            "kabupaten":      src.get("kabupaten"),
            "provinsi":       src.get("provinsi"),
            # QC fields — selalu include, None jika tidak ada
            "total_masalah":  qc_score.get("total_masalah"),
            "persen_bersih":  qc_score.get("persen_bersih"),
            "masalah_detail": src.get("content"),
        }
        return result

    @staticmethod
    def _extract_masalah_from_content(content: str) -> List[str]:
        """
        Ekstrak daftar masalah dari field content ES.

        Field content berformat:
        "... Masalah data: koordinat lintang kosong, foto sekolah belum diunggah."
        atau "Data lengkap." jika tidak ada masalah.

        Mengembalikan list masalah, atau list kosong jika tidak ada masalah.
        """
        if not content:
            return []
        import re
        m = re.search(r"Masalah data:\s*(.+?)\.?\s*$", content, re.IGNORECASE | re.DOTALL)
        if not m:
            return []
        masalah_str = m.group(1).strip()
        masalah_list = [m.strip() for m in masalah_str.split(",") if m.strip()]
        return masalah_list
