# services/query_handlers/aggregation_handler.py
import asyncio
import copy
import dataclasses
import json
import hashlib
import logging
import os
import re
from rapidfuzz import fuzz
from typing import Dict, Any, List, Optional

from elasticsearch import AsyncElasticsearch
from app.services.core.result_model import ResultStatus, build_result, build_ambiguous_message
from app.services.core.jenjang_constants import JENJANG_KEYWORDS
from app.services.core.location_resolver_adapter import (
    resolve_location_from_query,
    validate_location_presence,
    validate_hierarchy_consistency,
    PROVINCE_ALIAS_MAP,
)
from app.services.core.aggregation_core import (
    execute_aggregation_query,
    execute_global_ranking_query,
    execute_multi_aggregation_query,
    execute_nested_aggregation_query,
    execute_drilldown_query,
    normalize_entity_hierarchy,
    build_metric_aggs,
    build_distribusi_aggs,
    build_filters,
    build_qc_aggs,
    handle_qc_percentage_query as _handle_qc_percentage_query,
    PULAU_PROVINSI_MAP,
    _METRIC_NEEDS_PROFILE,
    _DISTRIBUSI_NEEDS_PROFILE,
)
from app.services.core.es_query_builder import (
    extract_filters_from_intent,
    build_aggregation_query,
    _NUMERIC_FIELD_MAP,
)
from app.services.confidence_engine import compute_aggregation_confidence 
from app.utils.cache import get_cache, set_cache

logger = logging.getLogger(__name__)


AGG_KEYWORDS = ("berapa", "jumlah", "total", "banyak")

# Variasi typo umum untuk kata kunci aggregation
AGG_KEYWORDS_FUZZY = (
    # Variasi "jumlah"
    "jumlh", "jmlah", "jmlh", "jumla", "jumlah",
    # Variasi "berapa"
    "brapa", "berpa", "brpa",
    # Variasi "banyak"
    "bnyak", "banak",
)

RANKING_KEYWORDS = (
    "terbanyak",
    "paling banyak",
    "tertinggi",
    "terbesar",
    "terbesar",
)


# Jenjang pendidikan sebagai pengganti kata "sekolah"
# JENJANG_KEYWORDS diimport dari jenjang_constants — single source of truth

# Kata yang menandakan niat LIST, bukan aggregation — skip langsung
_LIST_INTENT_WORDS = {
    "tampilkan", "daftar", "list", "cari", "carikan",
    "tunjukkan", "sebutkan", "lihat", "show",
}

# Pattern nama sekolah spesifik dengan angka
_SCHOOL_NAME_RE = re.compile(
    r'\b(sd|mi|smp|mts|sma|smk|ma|sman|smpn|sdn|smkn|man|mtsn|min)'
    r'\s*(?:negeri|swasta)?\s*\d',
    re.IGNORECASE
)
def is_aggregation_query(query: str) -> bool:
    q = query.lower()
    
    # GUARD 1: list intent
    if any(w in q.split() for w in _LIST_INTENT_WORDS):
        return False

    # GUARD 2: query nama sekolah spesifik (SMAN 3 vs SMAN 5, dll)
    # "jumlah siswa" di konteks nama sekolah bukan query aggregation lokasi
    if _SCHOOL_NAME_RE.search(q):
        return False
    
    # Deteksi apakah ada sinyal kuantitas
    has_agg_kw = any(k in q for k in AGG_KEYWORDS)
    has_agg_fuzzy = any(k in q for k in AGG_KEYWORDS_FUZZY)
    has_ranking = any(k in q for k in RANKING_KEYWORDS)

    if not (has_agg_kw or has_agg_fuzzy or has_ranking):
        # Cek fuzzy token-level
        tokens = q.split()
        has_agg_kw = any(
            (len(t) >= 3 and fuzz.ratio(t, "jumlah") >= 75) or
            (len(t) >= 4 and fuzz.ratio(t, "berapa") >= 75)
            for t in tokens
        )
        if not has_agg_kw:
            return False

    # Cek apakah ada "sekolah" atau jenjang pendidikan
    has_school_signal = (
        "sekolah" in q or
        any(j in q.split() for j in JENJANG_KEYWORDS) or
        # juga tangkap "sd," atau "smp," dengan koma
        any(j in re.sub(r"[^\w\s]", " ", q).split() for j in JENJANG_KEYWORDS)
    )

    # Metric keywords juga valid sebagai sinyal aggregation (tanpa kata "sekolah")
    # Contoh: "berapa guru PPPK di Jakarta?" / "berapa siswa perempuan di Jawa Barat?"
    _METRIC_SIGNAL_KW = {
        "guru", "siswa", "murid", "peserta didik", "tendik",
        "pppk", "p3k", "honorer", "pns", "asn", "sertifikasi",
        "komputer", "tenaga kependidikan",
    }
    has_metric_signal = any(k in q for k in _METRIC_SIGNAL_KW)

    return has_school_signal or has_metric_signal

def is_pure_ranking_query(query: str) -> bool:
    """
    Deteksi apakah query adalah ranking murni tanpa lokasi spesifik.
    Contoh: "provinsi mana yang memiliki sekolah terbanyak?"
            "kabupaten dengan sekolah terbanyak di indonesia"
    """
    q = query.lower()
    has_ranking = any(k in q for k in RANKING_KEYWORDS)
    has_level   = any(k in q for k in ("provinsi", "kabupaten", "kecamatan"))
    has_where   = any(k in q for k in ("mana", "di indonesia", "seluruh"))
    return has_ranking and has_level and (has_where or "sekolah" in q)

def is_ranking_query(query: str) -> bool:
    q = query.lower()
    return any(k in q for k in RANKING_KEYWORDS)


def _extract_ranking_size(query: str, default: int = 10, max_size: int = 20) -> int:
    """
    Ekstrak angka top-N dari ranking query.
    Contoh: "5 kabupaten dengan TK terbanyak" → 5
            "10 provinsi dengan SD terbanyak" → 10
            "kabupaten dengan SMK terbanyak"  → 10 (default)
    """
    m = re.search(
        r'\b(\d+)\s*(?:kabupaten|provinsi|kecamatan|kota)\b',
        query, re.IGNORECASE
    )
    if not m:
        m = re.search(r'\b(\d+)\s*(?:terbanyak|terbesar|tertinggi)\b', query, re.IGNORECASE)
    if m:
        return max(1, min(int(m.group(1)), max_size))
    return default

RANKING_LEVEL_KEYWORDS = {
    "provinsi": ["provinsi"],
    "kabupaten": ["kabupaten", "kota"],
    "kecamatan": ["kecamatan"],
    "desa_kelurahan": ["desa", "kelurahan"]
}

def detect_ranking_level(query: str) -> Optional[str]:
    q = query.lower()

    for level, keywords in RANKING_LEVEL_KEYWORDS.items():
        if any(k in q for k in keywords):
            return level

    # default fallback
    return None

# ===============================
# WILAYAH TRUST HELPERS
# ===============================

# Mapping canonical Jakarta → format "kota adm." yang disimpan di ES index
_JAKARTA_KABUPATEN_CANONICAL: dict = {
    "jakarta pusat":   "kota adm. jakarta pusat",
    "jakarta selatan": "kota adm. jakarta selatan",
    "jakarta barat":   "kota adm. jakarta barat",
    "jakarta timur":   "kota adm. jakarta timur",
    "jakarta utara":   "kota adm. jakarta utara",
    # "jakarta" bare is handled by PROVINCE_ALIAS_MAP → provinsi DKI, not kab
}

# Nama kota yang ambigu: ada kab. X dan kota X sekaligus.
# Untuk nama ini tanpa prefix eksplisit, jangan auto-trust → biarkan resolver
# ask for clarification via AMBIGUOUS response.
_AMBIGUOUS_CITIES = {
    "bandung", "malang", "tegal", "pekalongan", "probolinggo",
    "pasuruan", "mojokerto", "kediri", "blitar", "madiun",
    "magelang", "serang", "bekasi", "bogor", "depok", "tangerang",
    "sukabumi", "banjar", "cirebon", "tasikmalaya", "cimahi",
}

def _normalize_wilayah_prefix(value: str, prefix: str) -> str:
    """
    Kembalikan nilai wilayah dalam lowercase, tanpa menambah prefix ganda.
    Kalau sudah ada prefix administratif, kembalikan apa adanya.
    """
    if not value:
        return value
    val = value.lower().strip()
    known_prefixes = (
        "prov.", "kab.", "kec.", "kota ", "kabupaten ",
        "provinsi ", "kecamatan ",
    )
    if any(val.startswith(p) for p in known_prefixes):
        return val
    return f"{prefix} {val}"


# ===============================
# SUB-LEVEL MERGE HELPER
# ===============================
def _merge_sub_level_entities(resolution_entities: dict, all_matches: list) -> dict:
    """Pertahankan kecamatan/desa jika masih konsisten dengan kabupaten yang dipilih."""
    selected_kab = resolution_entities.get("kabupaten", "")
    for m in all_matches:
        if m["level"] == "kecamatan":
            hier = m.get("hierarchy", {})
            # hanya ambil kecamatan yang belong to kabupaten terpilih
            if hier.get("kabupaten", "").lower() == selected_kab.lower():
                resolution_entities["kecamatan"] = m["value"]
                break
    return resolution_entities



async def _handle_nasional_scope(
    query: str,
    es_client: AsyncElasticsearch,
    intent,
    resolution: dict,
    ranking_gap: float,
) -> Dict[str, Any]:
    """
    Handle agregasi nasional (tanpa filter wilayah).
    Diekstrak dari handle_aggregation_query (Phase 11).
    Returns dict dengan provider=ELASTICSEARCH_AGGREGATION_NASIONAL.
    """
    logger.info("[AGG] Scope nasional detected")

    _metric_target = getattr(intent, 'metric_target', None)
    _distribusi_field = getattr(intent, 'distribusi_field', None)
    _use_profile_index = (
        _metric_target in _METRIC_NEEDS_PROFILE
        or _distribusi_field in _DISTRIBUSI_NEEDS_PROFILE
    )
    index = (
        os.getenv("ELASTICSEARCH_PROFILE_INDEX", "idx_sekolah_profile")
        if _use_profile_index
        else os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")
    )

    query_filters = extract_filters_from_intent(intent)

    normalized: Dict[str, Any] = {}

    if query_filters.get("bentuk_pendidikan"):
        normalized["bentuk_pendidikan"] = query_filters["bentuk_pendidikan"]

    if query_filters.get("bentuk_pendidikan_in"):
        normalized["bentuk_pendidikan_in"] = query_filters["bentuk_pendidikan_in"]

    if query_filters.get("status_sekolah"):
        normalized["status_sekolah"] = query_filters["status_sekolah"]

    if query_filters.get("akreditasi_gte"):
        normalized["akreditasi_gte"] = query_filters["akreditasi_gte"]

    if query_filters.get("akreditasi_exact"):
        normalized["akreditasi_exact"] = query_filters["akreditasi_exact"]

    es_query = build_aggregation_query(normalized)

    es_query.setdefault("aggs", {})
    es_query["aggs"].update(build_metric_aggs(_metric_target))
    es_query["aggs"]["by_provinsi"] = {
        "terms": {
            "field": "provinsi",
            "size": 38
        }
    }
    if _distribusi_field:
        es_query["aggs"].update(build_distribusi_aggs(_distribusi_field))
    es_query["track_total_hits"] = True

    logger.debug("AGG NASIONAL QUERY: %s", es_query)

    result = await es_client.search(index=index, body=es_query)

    _aggs = result.get("aggregations", {})
    _total_raw = result.get("hits", {}).get("total", {})
    _total_doc = _total_raw.get("value", 0) if isinstance(_total_raw, dict) else _total_raw

    logger.debug("[AGG] by_status raw buckets: %s", _aggs.get("by_status", {}).get("buckets", []))
    logger.debug("[AGG] by_jenjang raw buckets count: %d", len(_aggs.get("by_jenjang", {}).get("buckets", [])))

    _main_metric_val = _aggs.get("main_metric", {}).get("value")
    _is_numeric = bool(_metric_target and _metric_target != "sekolah")
    if _is_numeric:
        total = int(_main_metric_val or 0)
    else:
        total = int(_main_metric_val or _total_doc)

    prov_buckets = _aggs.get("by_provinsi", {}).get("buckets", [])

    status_buckets = _aggs.get("by_status", {}).get("buckets", [])
    # Sum values per uppercase key — ES may have both "Swasta" and "SWASTA"
    # as separate keyword buckets; accumulate to avoid last-write-wins overwrite.
    breakdown_status_nasional: Dict[str, int] = {}
    for b in status_buckets:
        k = b.get("key", "").upper()
        if k in ("NEGERI", "SWASTA"):
            breakdown_status_nasional[k] = (
                breakdown_status_nasional.get(k, 0) + b["doc_count"]
            )

    jenjang_buckets = _aggs.get("by_jenjang", {}).get("buckets", [])
    breakdown_jenjang_nasional: Dict[str, int] = {
        b["key"]: b["doc_count"]
        for b in jenjang_buckets
        if b.get("key")
    }

    _distribusi_result = None
    if _distribusi_field:
        _dist_buckets = _aggs.get("distribusi", {}).get("buckets", [])
        _dist_total = sum(b["doc_count"] for b in _dist_buckets)
        _distribusi_result = [
            {
                "key": b["key"],
                "count": b["doc_count"],
                "pct": round(b["doc_count"] / _dist_total * 100, 1) if _dist_total > 0 else 0,
            }
            for b in _dist_buckets
        ]

    confidence = compute_aggregation_confidence(
        query=query,
        resolution_status=resolution["status"].value,
        resolution_metadata=resolution.get("metadata", {}),
        total_hits=total,
        ranking_gap=ranking_gap
    )

    answer_confident = confidence >= 0.7

    return {
        "status": ResultStatus.SUCCESS,
        "provider": "ELASTICSEARCH_AGGREGATION_NASIONAL",
        "total": total,
        "metric_target": _metric_target or "sekolah",
        "distribusi": _distribusi_result,
        "distribusi_field": _distribusi_field,
        "ranking": prov_buckets,
        "breakdown_status": breakdown_status_nasional or None,
        "breakdown_jenjang": breakdown_jenjang_nasional or None,
        "entities": {},
        "active_filters": query_filters,
        "confidence": confidence,
        "answer_confident": answer_confident,
        "metadata": resolution.get("metadata", {})
    }


async def _handle_multi_entity(
    query: str,
    es_client: AsyncElasticsearch,
    entities: list,
    resolution: dict,
    ranking_gap: float,
) -> Dict[str, Any]:
    """
    Handle agregasi multi-entitas (is_multi=True).
    Menjalankan execute_multi_aggregation_query + cache.
    """
    logger.info("MULTI AGGREGATION MODE: %s", entities)

    cache_key = _cache_key(entities)
    cached = await get_cache(cache_key)
    if cached:
        logger.info("MULTI CACHE HIT")
        return cached

    logger.debug(f"ENTITIES BEFORE QUERY: {entities}")
    results = await execute_multi_aggregation_query(es_client, entities)

    comparison = []
    for item in results:
        if not item:
            continue
        ent = item.get("entities", {})
        total = item.get("total", 0)
        comparison.append({"entities": ent, "total": total})

    total_sum = sum(x["total"] for x in comparison)
    if not comparison:
        return _validation("LOCATION_NOT_FOUND", query, resolution, ranking_gap)

    confidence = compute_aggregation_confidence(
        query=query,
        resolution_status=resolution["status"].value,
        resolution_metadata=resolution.get("metadata", {}),
        total_hits=total_sum,
        ranking_gap=ranking_gap
    )

    answer_confident = confidence >= 0.7

    result = {
        "status": ResultStatus.SUCCESS,
        "provider": "ELASTICSEARCH_MULTI_AGGREGATION",
        "comparison": comparison,
        "is_multi": True,
        "confidence": confidence,
        "answer_confident": answer_confident
    }

    await set_cache(cache_key, result, 3600)
    return result


def _format_ranking_result(
    query: str,
    buckets: list,
    confidence: float = 0.85,
    entities: Optional[Dict[str, Any]] = None,
    jenjang: Optional[str] = None,
) -> Dict[str, Any]:
    """Build standard ELASTICSEARCH_GLOBAL_RANKING result from ES terms-agg buckets."""
    top = buckets[0] if buckets else {}

    _q = query.lower()
    _top_level = "provinsi"  # default
    if any(w in _q for w in ("kabupaten", "kab.", "kota")):
        _top_level = "kabupaten"
    elif any(w in _q for w in ("kecamatan", "kec.")):
        _top_level = "kecamatan"

    return {
        "status": ResultStatus.SUCCESS,
        "provider": "ELASTICSEARCH_GLOBAL_RANKING",
        "top_location": top.get("key"),
        "top_level": _top_level,
        "top_jenjang": jenjang,
        "total": top.get("doc_count"),
        "ranking": buckets,
        "entities": entities or {},
        "jenjang": jenjang,
        "confidence": confidence,
        "answer_confident": confidence >= 0.7,
    }


async def _handle_ranking_fallback(
    query: str,
    es_client: AsyncElasticsearch,
    resolution: dict,
    ranking_gap: float,
    jenjang_filter: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Handle global ranking ketika tidak ada wilayah terdeteksi.
    Returns None jika tidak applicable.
    """
    if not is_ranking_query(query):
        return None

    level = detect_ranking_level(query)
    if not level:
        return None

    _sz = _extract_ranking_size(query, default=5)
    buckets = await execute_global_ranking_query(
        es_client, group_field=level, size=_sz, jenjang_filter=jenjang_filter
    )

    if not buckets:
        return None

    top = buckets[0]

    confidence = compute_aggregation_confidence(
        query=query,
        resolution_status=resolution["status"].value,
        resolution_metadata=resolution.get("metadata", {}),
        total_hits=top.get("doc_count", 0),
        ranking_gap=ranking_gap
    )

    return _format_ranking_result(
        query, buckets, confidence=confidence, jenjang=jenjang_filter
    )


async def _build_resolution(
    query: str,
    es_client: AsyncElasticsearch,
    intent,
    kwargs: dict,
) -> Dict[str, Any]:
    """Build resolution dict: trust intent scope/wilayah or call location resolver."""
    _scope = getattr(intent, "scope", None)

    if _scope == "pulau":
        w = getattr(intent, "wilayah", None)
        _pulau = (getattr(w, "pulau", None) or "").lower().strip() if w else ""
        # Fallback 1: explicit "pulau XXX" pattern
        if not _pulau:
            _m = re.search(r'\bpulau\s+(\w+)', query, re.IGNORECASE)
            if _m:
                _pulau = _m.group(1).lower().strip()
                logger.info("[AGG] pulau extracted from query (regex): %r", _pulau)
        # Fallback 2: scan known pulau names in query ("di Sulawesi" → "sulawesi")
        if not _pulau:
            _q_lower = query.lower()
            for _k in PULAU_PROVINSI_MAP:
                if _k in _q_lower:
                    _pulau = _k
                    logger.info("[AGG] pulau extracted from query (scan): %r", _pulau)
                    break
        if _pulau and _pulau in PULAU_PROVINSI_MAP:
            logger.info("[AGG] intent.scope=pulau, pulau=%s → multi-provinsi filter", _pulau)
            return {
                "status": ResultStatus.SUCCESS,
                "provider": "SYSTEM",
                "data": {"entities": {"pulau": _pulau}},
                "reason": None,
                "metadata": {"pulau_scope": True, "pulau": _pulau},
                "confidence": None,
            }
        logger.warning("[AGG] scope=pulau but pulau=%r not in PULAU_PROVINSI_MAP → fallback", _pulau)

    if _scope == "nasional":
        w = getattr(intent, "wilayah", None)
        has_specific_wilayah = w is not None and any([
            getattr(w, "provinsi", None),
            getattr(w, "kabupaten", None),
            getattr(w, "kecamatan", None),
            getattr(w, "desa", None),
        ])
        if not has_specific_wilayah:
            logger.info("[AGG] intent.scope=nasional (trusted) → skip location_resolver")
            return {
                "status": ResultStatus.SUCCESS,
                "provider": "SYSTEM",
                "data": {"entities": {}},
                "reason": None,
                "metadata": {"national_scope": True, "trigger": "intent_scope"},
                "confidence": None,
            }
        logger.warning("[AGG] intent.scope=nasional BUT wilayah present → fallback location_resolver")
        return await resolve_location_from_query(query, es_client)

    # Trust intent.wilayah — bypass resolver for specific wilayah
    _trusted = False
    if intent is not None:
        w = getattr(intent, "wilayah", None)
        if w is not None:
            wilayah_from_intent: Dict[str, str] = {}

            if getattr(w, "provinsi", None):
                _prov_bare = (w.provinsi or "").lower().strip()
                if _prov_bare in PULAU_PROVINSI_MAP:
                    logger.info("[AGG] wilayah.provinsi=%r is pulau name → redirect to pulau scope", _prov_bare)
                    return {
                        "status": ResultStatus.SUCCESS,
                        "provider": "SYSTEM",
                        "data": {"entities": {"pulau": _prov_bare}},
                        "reason": None,
                        "metadata": {"pulau_scope": True, "pulau": _prov_bare, "trigger": "provinsi_pulau_redirect"},
                        "confidence": None,
                    }
                wilayah_from_intent["provinsi"] = _normalize_wilayah_prefix(w.provinsi, "prov.")

            kab_raw = (getattr(w, "kabupaten", None) or "").lower().strip()
            if kab_raw:
                has_prefix = any(kab_raw.startswith(p) for p in ("kab.", "kota ", "kabupaten ", "kota adm."))
                logger.debug("[AGG] kab_raw=%r has_prefix=%s in_alias_map=%s",
                             kab_raw, has_prefix, kab_raw in PROVINCE_ALIAS_MAP)
                if not has_prefix and kab_raw in PROVINCE_ALIAS_MAP:
                    # Bare province alias in kabupaten field → upgrade to province scope
                    wilayah_from_intent["provinsi"] = PROVINCE_ALIAS_MAP[kab_raw]
                    logger.info("[AGG] Province alias upgrade: '%s' → provinsi='%s'",
                                kab_raw, PROVINCE_ALIAS_MAP[kab_raw])
                elif not has_prefix and kab_raw in _AMBIGUOUS_CITIES:
                    logger.info("[AGG] ambiguous city '%s' without prefix → skip auto-trust", kab_raw)
                elif kab_raw in _JAKARTA_KABUPATEN_CANONICAL:
                    canonical = _JAKARTA_KABUPATEN_CANONICAL[kab_raw]
                    wilayah_from_intent["kabupaten"] = canonical
                    logger.info("[AGG] Jakarta remap: '%s' → '%s'", kab_raw, canonical)
                else:
                    wilayah_from_intent["kabupaten"] = _normalize_wilayah_prefix(w.kabupaten, "kab.")

            if getattr(w, "kecamatan", None):
                wilayah_from_intent["kecamatan"] = _normalize_wilayah_prefix(w.kecamatan, "kec.")
            if getattr(w, "desa", None):
                wilayah_from_intent["desa_kelurahan"] = w.desa.lower().strip()

            if wilayah_from_intent:
                logger.info("[AGG] intent.wilayah=%s (trusted) → skip location_resolver", wilayah_from_intent)
                _trusted = True
                return {
                    "status": ResultStatus.SUCCESS,
                    "provider": "SYSTEM",
                    "data": {"entities": wilayah_from_intent},
                    "reason": None,
                    "metadata": {"trusted_from_intent": True, "scope": _scope},
                    "confidence": None,
                }

    # Diagnostic: metric queries without LLM-provided wilayah fall through here.
    # resolve_location_from_query strips noise words (komputer, listrik, etc.)
    # before looking up province aliases → should resolve Kalimantan Barat etc.
    _mt = getattr(intent, 'metric_target', None) if intent else None
    if _mt:
        logger.warning(
            "[AGG] metric_target=%r but wilayah empty → location resolver fallback for: '%s'",
            _mt, query[:60],
        )
    else:
        logger.info("[AGG] no trusted wilayah → location resolver for: '%s'", query[:60])
    return await resolve_location_from_query(query, es_client)


def _apply_entity_bridge(entities, data: dict, intent, kwargs: dict):
    """Apply sub-level merge + intent bridge to entities. Returns entities (possibly modified)."""
    # Sub-level merge: inject kecamatan from possible_matches
    if isinstance(entities, dict):
        all_matches = data.get("possible_matches", []) or data.get("matches", [])
        if all_matches:
            entities = _merge_sub_level_entities(entities, all_matches)
            logger.debug("[AGG] after sub-level merge: %s", entities)

    # EARLY INJECTION: intent fields → entities dict
    if intent is not None and isinstance(entities, dict):
        if getattr(intent, 'akreditasi', None):
            entities['akreditasi'] = intent.akreditasi
            entities['akreditasi_mode'] = getattr(intent, 'akreditasi_mode', 'exact') or 'exact'
        if getattr(intent, 'jenjang', None) and 'bentuk_pendidikan' not in entities:
            entities['bentuk_pendidikan'] = intent.jenjang
        if getattr(intent, 'keaktifan', None) is not None:
            entities['keaktifan'] = intent.keaktifan
        if getattr(intent, 'wilayah_terpencil', None) is not None:
            entities['wilayah_terpencil'] = intent.wilayah_terpencil
        if getattr(intent, 'wilayah_perbatasan', None) is not None:
            entities['wilayah_perbatasan'] = intent.wilayah_perbatasan
        if getattr(intent, 'partisipasi_bos', None) is not None:
            entities['partisipasi_bos'] = intent.partisipasi_bos
            logger.info("[AGG] partisipasi_bos filter: %s", intent.partisipasi_bos)
        if getattr(intent, 'qc_masalah_only', False):
            entities['qc_masalah_only'] = True
        if getattr(intent, 'qc_min_masalah', None) is not None:
            entities['qc_min_masalah'] = intent.qc_min_masalah
        if getattr(intent, 'qc_field', None) is not None:
            entities['qc_field'] = intent.qc_field
        if getattr(intent, 'qc_field_list', None):
            entities['qc_field_list'] = list(intent.qc_field_list)
        if getattr(intent, 'qc_persen_min', None) is not None:
            entities['qc_persen_min'] = float(intent.qc_persen_min)
        if getattr(intent, 'qc_persen_max', None) is not None:
            entities['qc_persen_max'] = float(intent.qc_persen_max)
        if getattr(intent, 'metric_target', None):
            entities['metric_target'] = intent.metric_target
        if getattr(intent, 'numeric_filters', None):
            entities['numeric_filters'] = intent.numeric_filters

    # Second bridge: via request.cognitive_intent or kwargs
    request_obj = kwargs.get("request")
    _intent = None
    if request_obj is not None:
        _intent = getattr(request_obj, "cognitive_intent", None)
    if _intent is None:
        _intent = intent or kwargs.get("cognitive_intent") or kwargs.get("intent")
    if _intent is not None and isinstance(entities, dict):
        if getattr(_intent, 'jenjang', None) and 'bentuk_pendidikan' not in entities:
            entities['bentuk_pendidikan'] = _intent.jenjang
            logger.debug("[AGG_BRIDGE] jenjang dari intent: %s", _intent.jenjang)
        if getattr(_intent, 'akreditasi', None):
            entities['akreditasi'] = _intent.akreditasi
            entities['akreditasi_mode'] = getattr(_intent, 'akreditasi_mode', 'exact') or 'exact'
            logger.debug("[AGG_BRIDGE] akreditasi: %s", _intent.akreditasi)

    logger.debug("DEBUG entities =%s", entities)
    return entities


async def _handle_single_wilayah(
    query: str,
    es_client: AsyncElasticsearch,
    entities: dict,
    intent,
    resolution: dict,
    ranking_gap: float,
) -> Dict[str, Any]:
    """
    Handle agregasi single-wilayah (path utama).
    Diekstrak dari handle_aggregation_query (Phase 11).
    Returns dict dengan provider=ELASTICSEARCH_AGGREGATION.
    """
    query_filters = extract_filters_from_intent(intent)

    cache_key = _cache_key({
        **entities,
        **{f"_qf_{k}": str(v) for k, v in query_filters.items()},
        "_metric": entities.get("metric_target", "sekolah"),
    })
    cached = await get_cache(cache_key)
    if cached:
        return cached

    logger.debug(f"ENTITIES BEFORE QUERY: {entities}")
    normalized = normalize_entity_hierarchy(entities)

    jenjang_label = None
    jenjang_requested = None

    if query_filters.get("bentuk_pendidikan"):
        normalized["bentuk_pendidikan"] = query_filters["bentuk_pendidikan"]
        logger.info("[AGG] filter jenjang: %s", query_filters["bentuk_pendidikan"])
        jenjang_label = query_filters["bentuk_pendidikan"]
        jenjang_requested = [jenjang_label]
    elif query_filters.get("bentuk_pendidikan_in"):
        multi_list = query_filters["bentuk_pendidikan_in"]
        normalized["bentuk_pendidikan_in"] = multi_list
        is_paud_expand = all(j in ["TK", "KB", "RA", "SPS", "TPA", "PAUDQ", "SDTK"] for j in multi_list)
        if is_paud_expand:
            jenjang_label = "PAUD"
            logger.info("[AGG] filter jenjang PAUD expand: %s", multi_list)
        else:
            jenjang_label = ", ".join(multi_list)
            logger.info("[AGG] filter multi jenjang: %s", multi_list)
        jenjang_requested = multi_list

    if not jenjang_requested:
        jenjang_requested = [jenjang_label] if jenjang_label else None

    if query_filters.get("status_sekolah"):
        normalized["status_sekolah"] = query_filters["status_sekolah"]
        logger.info("[AGG] filter status: %s", query_filters["status_sekolah"])

    if query_filters.get("akreditasi_gte"):
        normalized["akreditasi_gte"] = query_filters["akreditasi_gte"]
        logger.info("[AGG] filter akreditasi >=: %s", query_filters["akreditasi_gte"])
    elif query_filters.get("akreditasi_exact"):
        normalized["akreditasi_exact"] = query_filters["akreditasi_exact"]
        logger.info("[AGG] filter akreditasi =: %s", query_filters["akreditasi_exact"])

    current_level = None
    for lvl in ("desa_kelurahan", "kecamatan", "kabupaten", "provinsi", "pulau"):
        if lvl in normalized:
            current_level = lvl
            break

    use_nested = current_level in ("provinsi", "kabupaten", "pulau")
    DRILLDOWN_MAP = {"provinsi": "kabupaten", "kabupaten": "kecamatan"}
    drilldown_field = DRILLDOWN_MAP.get(current_level)

    breakdown_jenjang: Dict[str, int] = {}
    breakdown_status: Dict[str, int] = {}
    drilldown_top3 = []

    _metric_target = normalized.pop("metric_target", None) or entities.get("metric_target")
    _distribusi_field = getattr(intent, 'distribusi_field', None)
    _qc_masalah_only = getattr(intent, 'qc_masalah_only', False) or normalized.get('qc_masalah_only', False)
    _extra_qc_aggs: Dict[str, Any] = build_qc_aggs() if _qc_masalah_only else {}

    async def _run_metric_query() -> int:
        return await execute_aggregation_query(es_client, normalized)

    if use_nested:
        import asyncio as _asyncio
        nested_task = execute_nested_aggregation_query(
            es_client, normalized,
            metric_target=_metric_target,
            distribusi_field=_distribusi_field,
        )
        _drilldown_size = _extract_ranking_size(query) if is_ranking_query(query) else 3
        drilldown_task = (
            execute_drilldown_query(es_client, normalized, drilldown_field, size=_drilldown_size, metric_target=_metric_target)
            if drilldown_field else asyncio.sleep(0, result=[])
        )
        if _extra_qc_aggs:
            _qc_body = {
                "size": 0,
                "query": {"bool": {"filter": build_filters(normalized)}},
                "aggs": _extra_qc_aggs,
            }
            qc_task = es_client.search(
                index=os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc"),
                body=_qc_body,
                request_timeout=10,
            )
            nested_result, drilldown_top3, _qc_resp = await _asyncio.gather(
                nested_task, drilldown_task, qc_task, return_exceptions=True,
            )
            _qc_aggs_result = (
                _qc_resp.get("aggregations", {}) if not isinstance(_qc_resp, Exception) else {}
            )
        else:
            nested_result, drilldown_top3 = await _asyncio.gather(
                nested_task, drilldown_task, return_exceptions=True,
            )
            _qc_aggs_result = {}

        if isinstance(drilldown_top3, Exception):
            drilldown_top3 = []
        if drilldown_top3:
            logger.info("DRILLDOWN top3 field=%s results=%s", drilldown_field, drilldown_top3)

        if isinstance(nested_result, Exception) or nested_result is None:
            logger.warning("Nested agg failed, falling back to metric/count")
            total = await _run_metric_query()
        else:
            total = nested_result["total"]
            breakdown_jenjang = nested_result["breakdown_jenjang"]
            breakdown_status = nested_result["breakdown_status"]
            _distribusi_from_nested = nested_result.get("distribusi")
            logger.info("NESTED AGG total=%s jenjang_keys=%s status_keys=%s",
                total, list(breakdown_jenjang.keys())[:5], list(breakdown_status.keys()))
    else:
        total = await _run_metric_query()
        _qc_aggs_result = {}
        _distribusi_from_nested = None

    logger.info(f"AGG TOTAL = {total}")
    logger.info(f"AGG ENTITIES = {entities}")

    if total is None:
        return _error("ELASTICSEARCH_ERROR")

    _is_numeric_metric = _metric_target and _metric_target != "sekolah"
    has_operational_filter = bool(
        query_filters.get("akreditasi_gte") or
        query_filters.get("akreditasi_exact") or
        query_filters.get("bentuk_pendidikan") or
        query_filters.get("status_sekolah")
    )

    total_all = None
    pct_of_total = None
    if has_operational_filter and total is not None and total > 0 and not _is_numeric_metric:
        location_only = {
            k: v for k, v in normalized.items()
            if k not in (
                "bentuk_pendidikan", "status_sekolah",
                "akreditasi_gte", "akreditasi_exact", "_akreditasi_values"
            )
        }
        total_all = await execute_aggregation_query(es_client, location_only)
        if total_all and total_all > 0:
            pct_of_total = round(total / total_all * 100, 1)
            logger.info("[AGG] total=%s total_all=%s pct=%.1f%%", total, total_all, pct_of_total)

    if not jenjang_requested:
        jenjang_requested = query_filters.get("jenjang_requested", [])

    if jenjang_requested and breakdown_jenjang:
        requested_upper = [j.upper() for j in jenjang_requested]
        breakdown_jenjang_filtered = {
            k: v for k, v in breakdown_jenjang.items()
            if k.upper() in requested_upper
        }
        if breakdown_jenjang_filtered:
            breakdown_jenjang = breakdown_jenjang_filtered
            logger.info("[AGG] breakdown filtered to requested jenjang: %s", list(breakdown_jenjang.keys()))

    if total == 0:
        result = _validation("DATA_NOT_FOUND", query, resolution, ranking_gap, entities)
        logger.warning(f"ZERO RESULT for entities: {entities}")
        await set_cache(cache_key, result, 600)
        return result

    confidence = compute_aggregation_confidence(
        query=query,
        resolution_status=(
            resolution["status"].value
            if hasattr(resolution["status"], "value")
            else str(resolution["status"])
        ),
        resolution_metadata=resolution.get("metadata", {}),
        total_hits=total,
        ranking_gap=ranking_gap
    )

    answer_confident = confidence >= 0.7

    # ── QC detail: top masalah per kolom (hanya saat qc_masalah_only=True) ───
    _QC_COL_LABELS = {
        "qc_1": "Keunikan NPSN",    "qc_2": "Format NPSN",       "qc_3": "Nama sekolah",
        "qc_4": "Alamat jalan",     "qc_5": "Kode wilayah",      "qc_6": "Koordinat lintang",
        "qc_7": "Koordinat bujur",  "qc_8": "SK izin operasional","qc_9": "Tanggal SK izin",
        "qc_10": "SK pendirian",    "qc_11": "Tanggal SK pendirian","qc_12": "Foto sekolah",
        "qc_13": "Data yayasan",
    }
    qc_detail: Dict[str, Any] = {}
    if _qc_masalah_only and _qc_aggs_result:
        qc_detail["total_bermasalah"] = _qc_aggs_result.get("sekolah_bermasalah", {}).get("doc_count", 0)
        qc_detail["avg_masalah"] = round(
            _qc_aggs_result.get("avg_masalah", {}).get("value", 0) or 0, 1
        )
        _kolom = [
            (_qc_aggs_result.get(f"qc_{i}_masalah", {}).get("doc_count", 0), _QC_COL_LABELS[f"qc_{i}"])
            for i in range(1, 14)
            if _qc_aggs_result.get(f"qc_{i}_masalah", {}).get("doc_count", 0) > 0
        ]
        _kolom.sort(reverse=True)
        qc_detail["top_masalah"] = _kolom[:5]

    if qc_detail:
        logger.info("[QC_DETAIL] total_bermasalah=%s avg=%s top_masalah=%s",
                    qc_detail.get("total_bermasalah"), qc_detail.get("avg_masalah"),
                    qc_detail.get("top_masalah"))
    else:
        logger.info("[QC_DETAIL] empty — qc_masalah_only=%s aggs_keys=%s",
                    _qc_masalah_only, list(_qc_aggs_result.keys()) if _qc_aggs_result else [])

    result = {
        "status": ResultStatus.SUCCESS,
        "provider": "ELASTICSEARCH_AGGREGATION",
        "total": total,
        "metric_target": _metric_target or "sekolah",
        "distribusi": _distribusi_from_nested,
        "distribusi_field": _distribusi_field,
        "total_all": total_all,
        "pct_of_total": pct_of_total,
        "active_filters": query_filters,
        "jenjang_requested": jenjang_requested,
        "entities": entities,
        "confidence": confidence,
        "answer_confident": answer_confident,
        "metadata": resolution.get("metadata", {}),
        "breakdown_jenjang": breakdown_jenjang,
        "breakdown_status": breakdown_status,
        "drilldown_field": drilldown_field or "",
        "drilldown_top3": drilldown_top3,
        "qc_aggs": _qc_aggs_result,
        "qc_detail": qc_detail,
    }

    await set_cache(cache_key, result, 3600)
    return result


# ── Metric-target keyword patterns (ordered most-specific first) ─────────────
# Used to infer metric_target when Tier 2 classifies but doesn't extract it
_METRIC_KW_PATTERNS: list = [
    (re.compile(r'\bguru\s+p(?:3k|ppk?)\b|\bpppk\b|\bp3k\b', re.I), "guru_p3k"),
    (re.compile(r'\bguru\s+(?:laki[- ]?laki|pria|lk)\b', re.I),       "guru_l"),
    (re.compile(r'\bguru\s+(?:perempuan|wanita|pr)\b', re.I),           "guru_p_gender"),
    (re.compile(r'\b(?:total\s+)?komputer\b', re.I),                    "total_komputer"),
    (re.compile(r'\bsiswa\s+baru\b', re.I),                             "siswa_baru"),
    (re.compile(r'\bsiswa\s+mengulang\b', re.I),                        "siswa_mengulang"),
    (re.compile(r'\bsiswa\s+lulus\b', re.I),                            "siswa_lulus"),
    (re.compile(r'\bguru\s+(?:pns|asn)\b', re.I),                       "guru_pns"),
    (re.compile(r'\bguru\s+honor(?:er)?\b', re.I),                      "guru_honor"),
    (re.compile(r'\bguru\s+sertifika(?:si)?\b', re.I),                  "guru_sertifikasi"),
    (re.compile(r'\btendik\b|\btenaga\s+kependidikan\b', re.I),         "tendik"),
    (re.compile(r'\b(?:siswa|murid|peserta\s+didik)\b', re.I),          "siswa"),
    (re.compile(r'\bguru\b', re.I),                                     "guru"),
]

def _infer_metric_target(query: str) -> Optional[str]:
    """Infer metric_target from query text when Tier 2 didn't extract it."""
    for pattern, metric in _METRIC_KW_PATTERNS:
        if pattern.search(query):
            return metric
    return None


# Patterns for multi-metric inference — specific variants only, no generics.
# Generic "guru"/"siswa" intentionally excluded to avoid false positives
# (e.g. "berapa guru PPPK" must not become ['guru_p3k','guru']).
_MULTI_METRIC_PATTERNS: list = [
    (re.compile(r'\bsiswa\s+laki[- ]?laki\b|\bsiswa\s+lk\b', re.I), "siswa_l"),
    (re.compile(r'\bsiswa\s+perempuan\b|\bsiswa\s+pr\b', re.I),       "siswa_p"),
    (re.compile(r'\bguru\s+laki[- ]?laki\b|\bguru\s+lk\b', re.I),     "guru_l"),
    (re.compile(r'\bguru\s+perempuan\b|\bguru\s+pr\b', re.I),          "guru_p_gender"),
    (re.compile(r'\bguru\s+p(?:3k|ppk?)\b|\bpppk\b|\bp3k\b', re.I),  "guru_p3k"),
    (re.compile(r'\bguru\s+(?:pns|asn)\b', re.I),                      "guru_pns"),
    (re.compile(r'\bguru\s+honor(?:er)?\b', re.I),                     "guru_honor"),
    (re.compile(r'\bguru\s+sertifika(?:si)?\b', re.I),                 "guru_sertifikasi"),
    (re.compile(r'\bsiswa\s+baru\b|\bpeserta\s+didik\s+baru\b', re.I),"siswa_baru"),
    (re.compile(r'\bsiswa\s+lulus\b|\blulusan\b', re.I),               "siswa_lulus"),
    (re.compile(r'\bsiswa\s+mengulang\b|\btinggal\s+kelas\b|\btidak\s+naik\b', re.I), "siswa_mengulang"),
]


def _infer_metric_target_list(query: str) -> List[str]:
    """Infer multiple metric_targets from query when LLM didn't fill metric_target_list.

    Returns list of 2+ metrics if multi-metric signals found, else [].
    Used as fallback when Tier 2 classified the query but Tier 3 didn't run.
    """
    found: List[str] = []
    for pattern, metric in _MULTI_METRIC_PATTERNS:
        if pattern.search(query) and metric not in found:
            found.append(metric)
    return found if len(found) >= 2 else []


# ── Distribusi field keyword patterns ────────────────────────────────────────
# sumber_listrik (<0.04% coverage) dan akses_internet (<0.01% coverage) sengaja
# tidak dimasukkan — field ada di ES tapi coverage terlalu rendah untuk query user.
_DISTRIBUSI_PATTERNS: list = [
    (re.compile(r'\b(sumber.?air|air.?minum|air.?bersih|PAM|sumur|sungai|air.?yang.?dipakai)\b', re.I),
     "sumber_air"),
    (re.compile(r'\b(pagi|sore|waktu.?belajar|waktu.?penyelenggaraan|shift|sesi.?belajar)\b', re.I),
     "waktu_penyelenggaraan"),
    (re.compile(
        r'\b(status.?kepemilikan|milik.?(yayasan|pemda|pemerintah)|kepemilikan.?sekolah|dikelola.?(yayasan|pemerintah))\b',
        re.I),
     "status_kepemilikan"),
    (re.compile(
        r'\b(peserta.?bos|ikut.?bos|dapat.?bos|penerima.?bos|bantuan.?operasional|partisipasi.?bos)\b',
        re.I),
     "partisipasi_bos"),
]


def _infer_distribusi_field(query: str) -> Optional[str]:
    """Infer distribusi_field from query text when LLM/Tier2 didn't extract it."""
    q = query.lower()
    logger.debug("[DIST_INFER] query=%r", q[:80])
    for pattern, field in _DISTRIBUSI_PATTERNS:
        if pattern.search(q):
            logger.debug("[DIST_INFER] matched field=%s via pattern=%r", field, pattern.pattern[:50])
            return field
    logger.debug("[DIST_INFER] no match for query=%r", q[:80])
    return None


_MULTI_METRIC_LABELS: Dict[str, str] = {
    "siswa":            "siswa",
    "siswa_l":          "siswa laki-laki",
    "siswa_p":          "siswa perempuan",
    "siswa_baru":       "siswa baru",
    "siswa_mengulang":  "siswa mengulang",
    "siswa_lulus":      "siswa lulus",
    "guru":             "guru/PTK",
    "guru_pns":         "guru PNS/ASN",
    "guru_honor":       "guru honorer",
    "guru_sertifikasi": "guru bersertifikasi",
    "guru_s1":          "guru lulusan S1+",
    "guru_p3k":         "guru PPPK",
    "guru_l":           "guru laki-laki",
    "guru_p_gender":    "guru perempuan",
    "tendik":           "tenaga kependidikan",
    "total_komputer":   "komputer",
}


async def _handle_multi_metric_query(
    query: str,
    es_client: AsyncElasticsearch,
    intent,
    resolution: dict,
    ranking_gap: float,
    metric_list: List[str],
) -> Dict[str, Any]:
    """
    Jalankan aggregation paralel untuk setiap metric dalam metric_list.
    Dipanggil saat user minta dua atau lebih metric sekaligus (Phase 21-A).
    """
    data = resolution.get("data", {})
    entities_base = data.get("entities") or {}
    entities_base = _apply_entity_bridge(entities_base, data, intent, {})

    _WILAYAH_KEYS = ("provinsi", "kabupaten", "kecamatan", "desa_kelurahan", "pulau")
    _is_nasional = not any(entities_base.get(k) for k in _WILAYAH_KEYS)

    async def _run_one(metric: str):
        intent_copy = copy.copy(intent)
        intent_copy.metric_target = metric
        intent_copy.metric_target_list = []
        entities_copy = dict(entities_base)
        entities_copy["metric_target"] = metric
        try:
            if _is_nasional:
                # Nasional scope: _handle_single_wilayah dengan entities kosong hanya
                # melakukan count dokumen (execute_aggregation_query={}) tanpa metric sum.
                # _handle_nasional_scope membangun aggregation ES dengan build_metric_aggs()
                # yang benar untuk setiap metric.
                return await _handle_nasional_scope(
                    query, es_client, intent_copy, resolution, ranking_gap
                )
            return await _handle_single_wilayah(
                query, es_client, entities_copy, intent_copy, resolution, ranking_gap
            )
        except Exception as exc:
            logger.warning("[MULTI_METRIC] error for metric=%s: %s", metric, exc)
            return None

    results = await asyncio.gather(*[_run_one(m) for m in metric_list])

    multi_metric: Dict[str, Any] = {}
    for metric, result in zip(metric_list, results):
        if result and result.get("status") == ResultStatus.SUCCESS:
            multi_metric[metric] = {
                "total": result.get("total", 0),
                "label": _MULTI_METRIC_LABELS.get(metric, metric),
                "breakdown_jenjang": result.get("breakdown_jenjang", {}),
                "drilldown_top3": result.get("drilldown_top3", []),
                "drilldown_field": result.get("drilldown_field", ""),
            }

    if not multi_metric:
        return _validation("DATA_NOT_FOUND", query, resolution, ranking_gap, entities_base)

    confidence = compute_aggregation_confidence(
        query=query,
        resolution_status=(
            resolution["status"].value
            if hasattr(resolution["status"], "value")
            else str(resolution["status"])
        ),
        resolution_metadata=resolution.get("metadata", {}),
        total_hits=sum(v["total"] for v in multi_metric.values()),
        ranking_gap=ranking_gap,
    )

    return {
        "status": ResultStatus.SUCCESS,
        "provider": "ELASTICSEARCH_MULTI_METRIC",
        "multi_metric": multi_metric,
        "metric_target_list": metric_list,
        "entities": entities_base,
        "confidence": confidence,
        "answer_confident": confidence >= 0.7,
        "metadata": resolution.get("metadata", {}),
    }


async def handle_aggregation_query(
    query: str,
    es_client: AsyncElasticsearch,
    user_id: Optional[str] = None,
    intent=None,
    filters: Optional[Dict[str, Any]] = None,
    **kwargs
) -> Dict[str, Any]:
    # Pure ranking query → langsung ke global ranking
    if is_pure_ranking_query(query):
        level = detect_ranking_level(query)
        if level:
            _jf = getattr(intent, "jenjang", None) if intent else None
            _sz = _extract_ranking_size(query, default=5)
            buckets = await execute_global_ranking_query(
                es_client, group_field=level, size=_sz, jenjang_filter=_jf
            )
            if buckets:
                result = _format_ranking_result(query, buckets, confidence=0.85, jenjang=_jf)
                logger.info(
                    "[AGG] pure_ranking result: top_location=%s top_level=%s top_jenjang=%s",
                    result.get("top_location"), result.get("top_level"), result.get("top_jenjang"),
                )
                return result
    # ── Fallback: intent=None → assume nasional scope ────────────────────────
    if intent is None:
        from app.services.core.cognitive_router import CognitiveIntent, WilayahEntities
        intent = CognitiveIntent(
            module="aggregation",
            scope="nasional",
            secondary=[],
            sub_queries={},
            wilayah=WilayahEntities(),
        )

    # ── Early routing: qc_persen_query ───────────────────────────────────────
    if getattr(intent, "qc_persen_query", False):
        return await _handle_qc_percentage_query(intent, es_client, query)

    logger.info("[AGG] pre-inference: metric_target=%s metric_target_list=%s distribusi_field=%s query='%s'",
                getattr(intent, 'metric_target', None),
                getattr(intent, 'metric_target_list', []),
                getattr(intent, 'distribusi_field', None),
                query[:50])

    # ── Multi-metric FIRST: cek metric_target_list sebelum infer single metric ─
    _metric_list = getattr(intent, 'metric_target_list', []) or []

    # Fallback: infer dari query jika LLM (Tier 3) tidak mengisi metric_target_list
    # Ini terjadi saat query diklasifikasi oleh Tier 2 (tanpa LLM) atau LLM tidak return field ini
    if len(_metric_list) < 2:
        _inferred_list = _infer_metric_target_list(query)
        if len(_inferred_list) >= 2:
            _metric_list = _inferred_list
            logger.info("[AGG] metric_target_list inferred from query: %s", _metric_list)

    if len(_metric_list) >= 2:
        logger.info("[AGG] multi-metric query detected: %s", _metric_list)
        _mm_resolution = await _build_resolution(query, es_client, intent, kwargs)
        if _mm_resolution["status"] == ResultStatus.SUCCESS:
            return await _handle_multi_metric_query(
                query, es_client, intent, _mm_resolution, ranking_gap=0.0, metric_list=list(_metric_list)
            )
        # Jika resolution gagal, lanjut ke path normal (treat sebagai single metric atau nasional)

    # ── Infer metric_target from query if Tier 2 didn't extract it ───────────
    if not getattr(intent, 'metric_target', None):
        _inferred_metric = _infer_metric_target(query)
        if _inferred_metric:
            intent.metric_target = _inferred_metric
            logger.info("[AGG] metric_target inferred from query: %r → %r", query[:60], _inferred_metric)

    # ── Numeric filter + metric conflict: "sekolah dengan siswa > 300" means
    # COUNT of schools, not SUM(siswa) — override metric_target to None.
    _nf = getattr(intent, 'numeric_filters', []) or []
    if _nf and getattr(intent, 'metric_target', None):
        _metric_es  = _NUMERIC_FIELD_MAP.get(intent.metric_target, intent.metric_target)
        _filter_es  = {_NUMERIC_FIELD_MAP.get(f.field, f.field) for f in _nf}
        if _metric_es in _filter_es:
            logger.info("[NUMERIC_FILTER] metric_target=%r → None (filter=metric field, count sekolah)",
                        intent.metric_target)
            intent.metric_target = None

    # ── Infer distribusi_field from query if LLM didn't extract it ───────────
    if not getattr(intent, 'distribusi_field', None):
        _inferred_dist = _infer_distribusi_field(query)
        if _inferred_dist:
            intent.distribusi_field = _inferred_dist
            logger.info("[AGG] distribusi_field inferred from query: %r → %r", query[:60], _inferred_dist)

    resolution = await _build_resolution(query, es_client, intent, kwargs)
    logger.debug("[AGG] resolution=%s", resolution)
    ranking_gap = resolution.get("metadata", {}).get("ranking_gap", 0.0)
    
    if resolution["status"] == ResultStatus.NOT_FOUND:
        # Whitelist nasional keywords — paksa nasional scope
        _NATIONAL_HINT = re.compile(
            r"\b(indonesia|nasional|se-?indonesia|"
            r"seluruh\s+indonesia|nusantara|se-?nusantara)\b",
            re.IGNORECASE,
        )
        if _NATIONAL_HINT.search(query):
            logger.info("[AGG] National keyword detected → nasional scope")
            resolution = {
                "status": ResultStatus.SUCCESS,
                "provider": "SYSTEM",
                "data": {"entities": {}},
                "reason": None,
                "metadata": {
                    "national_scope": True,
                    "trigger": "keyword_whitelist",
                },
                "confidence": None,
            }
        else:
            _LOC_HINT = re.compile(
                r"\b(di|dari|provinsi|prov\.?|kabupaten|kab\.?|kota|"
                r"kecamatan|kec\.?|desa|kelurahan|wilayah)\b",
                re.IGNORECASE,
            )
            if _LOC_HINT.search(query):
                return _validation(
                    "LOCATION_NOT_FOUND",
                    query,
                    resolution,
                    ranking_gap
                )
            logger.info("[AGG] NOT_FOUND tanpa hint lokasi → fallback nasional scope")
            resolution = {
                "status": ResultStatus.SUCCESS,
                "provider": "SYSTEM",
                "data": {"entities": {}},
                "reason": None,
                "metadata": {"national_scope": True},
                "confidence": None,
            }

    if resolution["status"] == ResultStatus.VALIDATION_ERROR:
        return resolution

    # =====================================================
    # HANDLE AMBIGUOUS EARLY EXIT
    # =====================================================
    if resolution["status"] == ResultStatus.AMBIGUOUS:
        # ESCAPE HATCH: kalau query mengandung keyword nasional eksplisit,
        # override ambiguous resolution jadi nasional scope.
        _NATIONAL_HINT_AMB = re.compile(
            r"\b(indonesia|nasional|se-?indonesia|"
            r"seluruh\s+indonesia|nusantara|se-?nusantara)\b",
            re.IGNORECASE,
        )
        if _NATIONAL_HINT_AMB.search(query):
            logger.info(
                "[AGG] AMBIGUOUS resolution overridden by national keyword "
                "→ forcing nasional scope"
            )
            resolution = {
                "status": ResultStatus.SUCCESS,
                "provider": "SYSTEM",
                "data": {"entities": {}},
                "reason": None,
                "metadata": {
                    "national_scope": True,
                    "trigger": "ambiguous_override",
                },
                "confidence": None,
            }
        else:
            data = resolution.get("data", {})
            candidates = data.get("candidates", [])

            return build_result(
                status=ResultStatus.AMBIGUOUS,
                provider="SYSTEM_AMBIGUOUS",
                data={
                    "message": build_ambiguous_message(candidates),
                    "candidates": candidates
                },
                confidence=compute_aggregation_confidence(
                    query=query,
                    resolution_status=ResultStatus.AMBIGUOUS.value,
                    resolution_metadata=resolution.get("metadata", {}),
                    total_hits=0,
                    ranking_gap=ranking_gap
                )
            )

    if resolution["status"] != ResultStatus.SUCCESS:
        return resolution

    data = resolution.get("data", {})
    entities = data.get("entities")
    is_multi = data.get("is_multi", False)

    if entities is None:
        return _validation("LOCATION_NOT_FOUND", query, resolution, ranking_gap)

    entities = _apply_entity_bridge(entities, data, intent, kwargs)

    _jenjang_multi = getattr(intent, 'jenjang_list', []) or getattr(intent, 'jenjang_multi', [])
    if isinstance(entities, dict) and _jenjang_multi and len(_jenjang_multi) > 1:
        entities.pop('bentuk_pendidikan', None)
        entities['bentuk_pendidikan_in'] = _jenjang_multi
        logger.debug("[AGG] multi jenjang detected: %s", _jenjang_multi)
    # =====================================================
    # NASIONAL SCOPE HANDLING
    # =====================================================
    if isinstance(entities, dict):
        wilayah_keys = ("provinsi", "kabupaten", "kecamatan", "desa_kelurahan", "pulau")
        has_wilayah_filter = any(entities.get(k) for k in wilayah_keys)
        if not has_wilayah_filter:
            # Ranking query without location filter → global ranking (e.g. "5 provinsi dengan SD terbanyak")
            if is_ranking_query(query):
                level = detect_ranking_level(query)
                if level:
                    _jf = getattr(intent, "jenjang", None) if intent else None
                    _sz = _extract_ranking_size(query, default=5)
                    buckets = await execute_global_ranking_query(
                        es_client, group_field=level, size=_sz, jenjang_filter=_jf
                    )
                    if buckets:
                        return _format_ranking_result(query, buckets, confidence=0.85, jenjang=_jf)
            return await _handle_nasional_scope(query, es_client, intent, resolution, ranking_gap)
    # =====================================================
    # MULTI ENTITY HANDLING (WITH CACHE)
    # =====================================================

    if is_multi and isinstance(entities, list):
        return await _handle_multi_entity(query, es_client, entities, resolution, ranking_gap)

    if not validate_hierarchy_consistency(query, entities):
        # Ranking queries use level words ("kabupaten", "provinsi") as aggregation axis,
        # not as a location filter — e.g. "5 kabupaten dengan TK terbanyak di jawa timur".
        # Skip the mismatch guard so execution continues to the nested-agg path.
        if not is_ranking_query(query):
            return _validation(
                "LOCATION_LEVEL_MISMATCH",
                query,
                resolution,
                ranking_gap
            )
        logger.info(
            "[AGG] Skipping LOCATION_LEVEL_MISMATCH for ranking query: '%s'", query
        )

    # ===============================
    # AMBIGUITY HANDLING
    # ===============================
    if isinstance(entities, dict) and entities.get("ambiguous"):
        return {
            "provider": "SYSTEM_AMBIGUOUS",
            "total": 0,
            "entities": {},
            "options": entities.get("options", []),
            "reason": "AMBIGUOUS_LOCATION"
        }

    # ==================================
    # GLOBAL RANKING FALLBACK
    # ==================================
    if not validate_location_presence(entities):
        _jf_fb = getattr(intent, "jenjang", None) if intent else None
        fallback = await _handle_ranking_fallback(
            query, es_client, resolution, ranking_gap, jenjang_filter=_jf_fb
        )
        if fallback is not None:
            return fallback
        return _validation("LOCATION_NOT_FOUND", query, resolution, ranking_gap)

    # ==================================
    # FILTERED RANKING
    # "10 kabupaten dengan SMA terbanyak di jawa timur" → rank kabupaten within jawa timur
    # entities has parent-scope (e.g. provinsi) but level is the ranking axis (kabupaten).
    # ==================================
    if is_ranking_query(query):
        level = detect_ranking_level(query)
        if level and isinstance(entities, dict) and level not in entities:
            _jf  = getattr(intent, "jenjang", None) if intent else None
            _pf  = entities.get("provinsi")
            _sz  = _extract_ranking_size(query, default=5)
            buckets = await execute_global_ranking_query(
                es_client, group_field=level, size=_sz,
                jenjang_filter=_jf, provinsi_filter=_pf,
            )
            if buckets:
                return _format_ranking_result(
                    query, buckets, confidence=0.85, entities=entities, jenjang=_jf
                )

    return await _handle_single_wilayah(query, es_client, entities, intent, resolution, ranking_gap)

def _cache_key(entities: Any) -> str:
    def _default(obj):
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    raw = json.dumps(entities, sort_keys=True, ensure_ascii=False, default=_default)
    prefix = "agg_multi:" if isinstance(entities, list) else "agg:"
    return f"{prefix}{hashlib.md5(raw.encode()).hexdigest()}"



def _validation(
    reason: str,
    query: str,
    resolution: Dict[str, Any],
    ranking_gap: float,
    entities: Optional[Dict[str, Any]] = None
):

    confidence = compute_aggregation_confidence(
        query=query,
        resolution_status=resolution["status"].value,
        resolution_metadata=resolution.get("metadata", {}),
        total_hits=0,
        ranking_gap=ranking_gap
    )

    return build_result(
        status=ResultStatus.VALIDATION_ERROR,
        provider="SYSTEM_VALIDATION",
        reason=reason,
        confidence=confidence,
        data={"entities": entities or {}}
    )


def _error(reason: str):
    return build_result(
        status=ResultStatus.VALIDATION_ERROR,
        provider="SYSTEM_ERROR",
        reason=reason
    )
