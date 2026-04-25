# services/query_handlers/aggregation_handler.py
import asyncio
import json
import hashlib
import logging
import os
import re
from rapidfuzz import fuzz
from typing import Dict, Any, Optional

from elasticsearch import AsyncElasticsearch
from app.services.core.result_model import ResultStatus, build_result, build_ambiguous_message
from app.services.core.jenjang_constants import JENJANG_KEYWORDS
from app.services.entity_service import (
    resolve_entities,
    validate_location_presence,
    validate_hierarchy_consistency
)
from app.services.core.aggregation_core import (
    execute_aggregation_query,
    execute_global_ranking_query,
    execute_multi_aggregation_query,
    execute_nested_aggregation_query,
    execute_drilldown_query,
    normalize_entity_hierarchy,
    build_metric_aggs,
    build_filters,
    build_qc_aggs,
)
from app.services.core.es_query_builder import (
    extract_filters_from_intent,
    build_aggregation_query,
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

    return has_school_signal

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

async def _handle_qc_percentage_query(
    intent,
    es_client: AsyncElasticsearch,
    query: str = "",
) -> Dict[str, Any]:
    """
    Hitung persentase sekolah bersih / bermasalah dari ES.

    Menjalankan dua query:
      1. Total semua sekolah (dengan filter wilayah+jenjang, tanpa filter QC)
      2. Total sekolah bersih (total_masalah == 0)
    Kemudian menghitung proporsinya.
    """
    from app.services.core.es_query_builder import extract_filters_from_intent, build_es_query
    index = os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc")

    # Wilayah label untuk narasi
    w = getattr(intent, "wilayah", None)
    if w and getattr(w, "kabupaten", None):
        wilayah_label = w.kabupaten.title()
    elif w and getattr(w, "provinsi", None):
        wilayah_label = w.provinsi.title()
    else:
        wilayah_label = "seluruh Indonesia"

    es_filters = extract_filters_from_intent(intent)

    try:
        # Query 1: total semua sekolah
        q_total = build_es_query(intent, es_filters, size=0)
        q_total["aggs"] = {"total": {"value_count": {"field": "npsn"}}}

        # Query 2: sekolah bersih (total_masalah = 0)
        q_bersih = build_es_query(intent, es_filters, size=0)
        q_bersih["query"]["bool"].setdefault("filter", [])
        q_bersih["query"]["bool"]["filter"].append(
            {"term": {"qc_score.total_masalah": 0}}
        )
        q_bersih["aggs"] = {"total": {"value_count": {"field": "npsn"}}}

        r_total, r_bersih = await asyncio.gather(
            es_client.search(index=index, body=q_total, request_timeout=10),
            es_client.search(index=index, body=q_bersih, request_timeout=10),
        )

        total = r_total.get("aggregations", {}).get("total", {}).get("value", 0)
        bersih = r_bersih.get("aggregations", {}).get("total", {}).get("value", 0)
        bermasalah = total - bersih

        persen_bersih = round((bersih / total) * 100, 2) if total > 0 else 0.0
        persen_bermasalah = round((bermasalah / total) * 100, 2) if total > 0 else 0.0

        return {
            "status": ResultStatus.SUCCESS,
            "provider": "ELASTICSEARCH_QC_PERCENTAGE",
            "data": {
                "qc_percentage": True,
                "total_sekolah": total,
                "total_bersih": bersih,
                "total_bermasalah": bermasalah,
                "persen_bersih": persen_bersih,
                "persen_bermasalah": persen_bermasalah,
                "wilayah_label": wilayah_label,
            },
            "confidence": 0.95,
            "answer_confident": True,
        }
    except Exception as exc:
        logger.exception("[AGG_QC_PCT] Error: %s", exc)
        return {
            "status": ResultStatus.VALIDATION_ERROR,
            "provider": "SYSTEM_VALIDATION",
            "data": {"message": "Gagal menghitung persentase kualitas data."},
            "confidence": 0,
            "answer_confident": False,
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
            buckets = await execute_global_ranking_query(
                es_client, group_field=level, size=5
            )
            if buckets:
                top = buckets[0]
                confidence = 0.85
                return {
                    "status": ResultStatus.SUCCESS,
                    "provider": "ELASTICSEARCH_GLOBAL_RANKING",
                    "top_location": top.get("key"),
                    "total": top.get("doc_count"),
                    "ranking": buckets,
                    "confidence": confidence,
                    "answer_confident": True
                }
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

    # =====================================================
    # TRUST INTENT.SCOPE — short-circuit untuk nasional
    # =====================================================
    _scope = getattr(intent, "scope", None) if intent is not None else None

    if _scope == "nasional":
        w = getattr(intent, "wilayah", None)
        has_specific_wilayah = w is not None and any([
            getattr(w, "provinsi", None),
            getattr(w, "kabupaten", None),
            getattr(w, "kecamatan", None),
            getattr(w, "desa", None),
        ])
        if not has_specific_wilayah:
            logger.info(
                "[AGG] intent.scope=nasional (trusted from cognitive router) "
                "→ skip resolve_entities"
            )
            resolution = {
                "status": ResultStatus.SUCCESS,
                "provider": "SYSTEM",
                "data": {"entities": {}},
                "reason": None,
                "metadata": {
                    "national_scope": True,
                    "trigger": "intent_scope",
                },
                "confidence": None,
            }
        else:
            logger.warning(
                "[AGG] intent.scope=nasional BUT wilayah present "
                "(prov=%s kab=%s kec=%s) → fallback resolve_entities",
                getattr(w, "provinsi", None),
                getattr(w, "kabupaten", None),
                getattr(w, "kecamatan", None),
            )
            resolution = await resolve_entities(query, es_client)
    else:
        # =====================================================
        # TRUST INTENT.WILAYAH — bypass resolver untuk wilayah spesifik
        # =====================================================
        _trusted = False
        if intent is not None:
            w = getattr(intent, "wilayah", None)
            if w is not None:
                wilayah_from_intent: Dict[str, str] = {}

                if getattr(w, "provinsi", None):
                    wilayah_from_intent["provinsi"] = _normalize_wilayah_prefix(
                        w.provinsi, "prov."
                    )

                kab_raw = (getattr(w, "kabupaten", None) or "").lower().strip()
                if kab_raw:
                    has_prefix = any(
                        kab_raw.startswith(p)
                        for p in ("kab.", "kota ", "kabupaten ")
                    )
                    if not has_prefix and kab_raw in _AMBIGUOUS_CITIES:
                        logger.info(
                            "[AGG] ambiguous city '%s' without prefix → "
                            "skip auto-trust, let resolver ask for clarification",
                            kab_raw,
                        )
                    else:
                        wilayah_from_intent["kabupaten"] = _normalize_wilayah_prefix(
                            w.kabupaten, "kab."
                        )

                if getattr(w, "kecamatan", None):
                    wilayah_from_intent["kecamatan"] = _normalize_wilayah_prefix(
                        w.kecamatan, "kec."
                    )
                if getattr(w, "desa", None):
                    wilayah_from_intent["desa_kelurahan"] = w.desa.lower().strip()

                if wilayah_from_intent:
                    logger.info(
                        "[AGG] intent.wilayah=%s (trusted from cognitive router) "
                        "→ skip resolve_entities",
                        wilayah_from_intent,
                    )
                    resolution = {
                        "status": ResultStatus.SUCCESS,
                        "provider": "SYSTEM",
                        "data": {"entities": wilayah_from_intent},
                        "reason": None,
                        "metadata": {
                            "trusted_from_intent": True,
                            "scope": _scope,
                        },
                        "confidence": None,
                    }
                    _trusted = True

        if not _trusted:
            resolution = await resolve_entities(query, es_client)

    # ===============================
    # DEBUG
    # ===============================
    logger.debug("[AGG_DEBUG] resolution=%s", resolution)
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
    
    # ===============================
    # SUB-LEVEL MERGE (kecamatan dari possible_matches)
    # ===============================
    if isinstance(entities, dict):
        all_matches = data.get("possible_matches", []) or data.get("matches", [])
        if all_matches:
            entities = _merge_sub_level_entities(entities, all_matches)
            logger.debug("[AGG] after sub-level merge: %s", entities)
            
    # ── Bridge dari intent → entities (EARLY INJECTION) ─────────────
    if intent is not None and isinstance(entities, dict):
        if getattr(intent, 'akreditasi', None):
            entities['akreditasi'] = intent.akreditasi
            entities['akreditasi_mode'] = getattr(intent, 'akreditasi_mode', 'exact') or 'exact'
            logger.debug("[AGG_BRIDGE_EARLY] akreditasi: %s", intent.akreditasi)

        if getattr(intent, 'jenjang', None) and 'bentuk_pendidikan' not in entities:
            entities['bentuk_pendidikan'] = intent.jenjang
            logger.debug("[AGG_BRIDGE_EARLY] jenjang: %s", intent.jenjang)

        if getattr(intent, 'keaktifan', None) is not None:
            entities['keaktifan'] = intent.keaktifan
            logger.debug("[AGG_BRIDGE_EARLY] keaktifan: %s", intent.keaktifan)

        if getattr(intent, 'wilayah_terpencil', None) is not None:
            entities['wilayah_terpencil'] = intent.wilayah_terpencil
            logger.debug("[AGG_BRIDGE_EARLY] wilayah_terpencil: %s", intent.wilayah_terpencil)

        if getattr(intent, 'wilayah_perbatasan', None) is not None:
            entities['wilayah_perbatasan'] = intent.wilayah_perbatasan
            logger.debug("[AGG_BRIDGE_EARLY] wilayah_perbatasan: %s", intent.wilayah_perbatasan)

        if getattr(intent, 'qc_masalah_only', False):
            entities['qc_masalah_only'] = True
            logger.debug("[AGG_BRIDGE_EARLY] qc_masalah_only=True")

        if getattr(intent, 'qc_min_masalah', None) is not None:
            entities['qc_min_masalah'] = intent.qc_min_masalah
            logger.debug("[AGG_BRIDGE_EARLY] qc_min_masalah: %s", intent.qc_min_masalah)

        if getattr(intent, 'metric_target', None):
            entities['metric_target'] = intent.metric_target
            logger.debug("[AGG_BRIDGE_EARLY] metric_target: %s", intent.metric_target)
    # ── BRIDGE: merge intent → entities (akreditasi & jenjang) ─────────────
    if intent:

        def _inject(e):
            if not isinstance(e, dict):
                return e
            if getattr(intent, "akreditasi", None):
                e["akreditasi"] = intent.akreditasi
            if getattr(intent, "akreditasi_mode", None):
                e["akreditasi_mode"] = intent.akreditasi_mode
            if getattr(intent, "jenjang", None):
                e["bentuk_pendidikan"] = intent.jenjang
            return e

        if isinstance(entities, list):
            entities = [_inject(e) for e in entities]
        elif isinstance(entities, dict):
            entities = _inject(entities)
    
    if entities is None:
        return _validation(
            "LOCATION_NOT_FOUND",
            query,
            resolution,
            ranking_gap
        )
    # ── Bridge filter dari CognitiveIntent → entities ────────────────────────────
    # intent bisa berupa CognitiveIntent (LLM path) atau None (fallback path)
    request_obj = kwargs.get("request")
    _intent = None

    if request_obj is not None:
        _intent = getattr(request_obj, "cognitive_intent", None)

    if _intent is None:
        _intent = intent or kwargs.get("cognitive_intent") or kwargs.get("intent")

    if _intent is not None:
        # Merge jenjang dari intent jika belum ada di entities
        if getattr(_intent, 'jenjang', None) and 'bentuk_pendidikan' not in entities:
            entities['bentuk_pendidikan'] = _intent.jenjang
            logger.debug("[AGG_BRIDGE] jenjang dari intent: %s", _intent.jenjang)

        # Merge akreditasi dari intent
        if getattr(_intent, 'akreditasi', None):
            entities['akreditasi'] = _intent.akreditasi
            entities['akreditasi_mode'] = getattr(_intent, 'akreditasi_mode', 'exact') or 'exact'
            logger.debug(
                "[AGG_BRIDGE] akreditasi dari intent: %s mode=%s",
                _intent.akreditasi, entities['akreditasi_mode']
            )
    logger.debug("DEBUG entities =%s", entities)
    
    # ===============================
    # MULTI-JENJANG HANDLING (FROM INTENT)
    # ===============================
    _jenjang_multi = getattr(intent, 'jenjang_list', []) or getattr(intent, 'jenjang_multi', [])

    if isinstance(entities, dict) and _jenjang_multi and len(_jenjang_multi) > 1:
        # Hapus jenjang tunggal kalau ada
        entities.pop('bentuk_pendidikan', None)

        # Gunakan filter IN untuk multi jenjang
        entities['bentuk_pendidikan_in'] = _jenjang_multi

        logger.debug("[AGG] multi jenjang detected: %s", _jenjang_multi)


    # ===============================
    # PERSENTASE SUPPORT (DENOMINATOR PREP)
    # ===============================
    _needs_pct = bool(
        entities.get('akreditasi') or
        entities.get('akreditasi_gte') or
        entities.get('akreditasi_exact')
    )

    entities_no_akred = None

    if _needs_pct and isinstance(entities, dict):
        # Copy tanpa filter akreditasi
        entities_no_akred = {
            k: v for k, v in entities.items()
            if k not in (
                'akreditasi',
                'akreditasi_mode',
                'akreditasi_gte',
                'akreditasi_exact'
            )
        }

        logger.debug("[AGG] entities_no_akred prepared: %s", entities_no_akred)
    # =====================================================
    # NASIONAL SCOPE HANDLING
    # =====================================================
    # Tidak ada filter wilayah sama sekali → agregasi nasional
    if isinstance(entities, dict):

        wilayah_keys = (
            "provinsi",
            "kabupaten",
            "kecamatan",
            "desa_kelurahan"
        )

        has_wilayah_filter = any(
            entities.get(k) for k in wilayah_keys
        )

        if not has_wilayah_filter:

            logger.info("[AGG] Scope nasional detected")

            query_filters = extract_filters_from_intent(intent)

            normalized = {}

            # inject filter operasional (jenjang, status, akreditasi)
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

            # build ES aggregation query
            es_query = build_aggregation_query(normalized)

            # tambahan breakdown nasional per provinsi
            es_query.setdefault("aggs", {})
            es_query["aggs"]["by_provinsi"] = {
                "terms": {
                    "field": "provinsi",
                    "size": 38
                }
            }

            logger.debug(
                "AGG NASIONAL QUERY: %s",
                es_query
            )

            result = await es_client.search(
                index=os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc"),
                body=es_query
            )

            total = (
                result.get("aggregations", {})
                .get("total", {})
                .get("value", 0)
            )

            prov_buckets = (
                result.get("aggregations", {})
                .get("by_provinsi", {})
                .get("buckets", [])
            )

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
                "ranking": prov_buckets,
                "entities": {},
                "active_filters": query_filters,
                "confidence": confidence,
                "answer_confident": answer_confident,
                "metadata": resolution.get("metadata", {})
            }
    # =====================================================
    # MULTI ENTITY HANDLING (WITH CACHE)
    # =====================================================

    if is_multi and isinstance(entities, list):

        logger.info("MULTI AGGREGATION MODE: %s", entities)

        # ---- CACHE CHECK ----
        cache_key = _cache_key(entities)
        cached = await get_cache(cache_key)
        if cached:
            logger.info("MULTI CACHE HIT")
            return cached

        # ---- EXECUTE MULTI ----
        logger.debug(f"ENTITIES BEFORE QUERY: {entities}")
        results = await execute_multi_aggregation_query(
            es_client,
            entities
        )

        comparison = []

        for item in results:
            if not item:
                continue

            ent = item.get("entities", {})
            total = item.get("total", 0)

            comparison.append({
                "entities": ent,
                "total": total
            })

        total_sum = sum(x["total"] for x in comparison)
        if not comparison:
            return _validation(
                "LOCATION_NOT_FOUND",
                query,
                resolution,
                ranking_gap
            )

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

        # ---- SAVE CACHE ----
        await set_cache(cache_key, result, 3600)

        return result


    if not validate_hierarchy_consistency(query, entities):
        return _validation(
            "LOCATION_LEVEL_MISMATCH",
            query,
            resolution,
            ranking_gap
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

        if is_ranking_query(query):
            level = detect_ranking_level(query)

            if not level:
                return _validation(
                    "LOCATION_NOT_FOUND",
                    query,
                    resolution,
                    ranking_gap
                )

            group_field = level

            buckets = await execute_global_ranking_query(
                es_client,
                group_field=group_field,
                size=5
            )

            if not buckets:
                return _validation(
                    "LOCATION_NOT_FOUND",
                    query,
                    resolution,
                    ranking_gap
                )

            top = buckets[0]

            confidence = compute_aggregation_confidence(
                query=query,
                resolution_status=resolution["status"].value,
                resolution_metadata=resolution.get("metadata", {}),
                total_hits=top.get("doc_count", 0),
                ranking_gap=ranking_gap
            )

            answer_confident = confidence >= 0.7


            return {
                "status": ResultStatus.SUCCESS,
                "provider": "ELASTICSEARCH_GLOBAL_RANKING",
                "top_location": top.get("key"),
                "total": top.get("doc_count"),
                "ranking": buckets,
                "confidence": confidence,
                "answer_confident": answer_confident
            }
            
        return _validation(
            "LOCATION_NOT_FOUND",
            query,
            resolution,
            ranking_gap
        )


    # Cache key harus mencakup query filters agar tidak cache collision
    # antara query "berapa sekolah di sleman" vs "berapa sekolah akreditasi B di sleman"
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

    # ── Inject filter operasional dari query ─────────────────────────────────

    jenjang_label = None
    jenjang_requested = None
    
    if query_filters.get("bentuk_pendidikan"):

        normalized["bentuk_pendidikan"] = query_filters["bentuk_pendidikan"]

        logger.info(
            "[AGG] filter jenjang: %s",
            query_filters["bentuk_pendidikan"]
        )

        jenjang_label = query_filters["bentuk_pendidikan"]
        jenjang_requested = [jenjang_label]
        
    elif query_filters.get("bentuk_pendidikan_in"):

        multi_list = query_filters["bentuk_pendidikan_in"]
        normalized["bentuk_pendidikan_in"] = multi_list

        # PAUD expand (TK, KB, RA, dll.)
        is_paud_expand = all(
            j in ["TK", "KB", "RA", "SPS", "TPA", "PAUDQ", "SDTK"]
            for j in multi_list
        )

        if is_paud_expand:
            jenjang_label = "PAUD"
            logger.info("[AGG] filter jenjang PAUD expand: %s", multi_list)
        else:
            # Multi-jenjang eksplisit user
            jenjang_label = ", ".join(multi_list)
            logger.info("[AGG] filter multi jenjang: %s", multi_list)

        #  INI PENTING BANGET
        jenjang_requested = multi_list

    # pass ke narrator
    if not jenjang_requested:
        jenjang_requested = [jenjang_label] if jenjang_label else None

    if query_filters.get("status_sekolah"):

        normalized["status_sekolah"] = query_filters["status_sekolah"]

        logger.info(
            "[AGG] filter status: %s",
            query_filters["status_sekolah"]
        )
    # ─────────────────────────────────────────────────────────────────────────
    if query_filters.get("akreditasi_gte"):
        normalized["akreditasi_gte"] = query_filters["akreditasi_gte"]
        logger.info("[AGG] filter akreditasi >=: %s", query_filters["akreditasi_gte"])
    elif query_filters.get("akreditasi_exact"):
        normalized["akreditasi_exact"] = query_filters["akreditasi_exact"]
        logger.info("[AGG] filter akreditasi =: %s", query_filters["akreditasi_exact"])
    # ── Tentukan level saat ini ─────────────────────────────────────────────
    # Urutan prioritas: desa > kecamatan > kabupaten > provinsi
    current_level = None
    for lvl in ("desa_kelurahan", "kecamatan", "kabupaten", "provinsi"):
        if lvl in normalized:
            current_level = lvl
            break

    # ── Nested aggregation: aktif untuk provinsi dan kabupaten ───────────────
    # Level ini punya data cukup banyak sehingga breakdown bermakna.
    # Kecamatan dan desa tetap pakai count API (lebih cepat).
    use_nested = current_level in ("provinsi", "kabupaten")

    # ── Drilldown level mapping ──────────────────────────────────────────────
    # Provinsi → top 3 kabupaten/kota
    # Kabupaten → top 3 kecamatan
    # Kecamatan → top 3 desa (opsional, data mungkin terlalu granular)
    DRILLDOWN_MAP = {
        "provinsi":  "kabupaten",
        "kabupaten": "kecamatan",
    }
    drilldown_field = DRILLDOWN_MAP.get(current_level)

    breakdown_jenjang = {}
    breakdown_status  = {}
    drilldown_top3    = []

    # Ekstrak metric_target sebelum cabang use_nested / kecamatan
    _metric_target = normalized.pop("metric_target", None) or entities.get("metric_target")

    # ── QC aggs: aktif jika intent minta data bermasalah ────────────────────
    _qc_masalah_only = getattr(intent, 'qc_masalah_only', False) or normalized.get('qc_masalah_only', False)
    _extra_qc_aggs: Dict[str, Any] = build_qc_aggs() if _qc_masalah_only else {}

    async def _run_metric_query() -> int:
        """Count dokumen sekolah — index baru hanya mendukung count."""
        return await execute_aggregation_query(es_client, normalized)

    if use_nested:
        # Provinsi/Kabupaten: nested agg + drilldown berjalan concurrent
        import asyncio as _asyncio
        nested_task = execute_nested_aggregation_query(
            es_client, normalized, metric_target=_metric_target
        )
        drilldown_task = (
            execute_drilldown_query(es_client, normalized, drilldown_field, size=3)
            if drilldown_field
            else asyncio.sleep(0, result=[])
        )
        # QC aggs (opsional, hanya jika qc_masalah_only)
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
                nested_task, drilldown_task, qc_task,
                return_exceptions=True,
            )
            _qc_aggs_result = (
                _qc_resp.get("aggregations", {})
                if not isinstance(_qc_resp, Exception) else {}
            )
        else:
            nested_result, drilldown_top3 = await _asyncio.gather(
                nested_task, drilldown_task,
                return_exceptions=True,
            )
            _qc_aggs_result = {}

        if isinstance(drilldown_top3, Exception):
            drilldown_top3 = []
        if drilldown_top3:
            logger.info(
                "DRILLDOWN top3 field=%s results=%s",
                drilldown_field, drilldown_top3
            )

        if isinstance(nested_result, Exception) or nested_result is None:
            logger.warning("Nested agg failed, falling back to metric/count")
            total = await _run_metric_query()
        else:
            total             = nested_result["total"]
            breakdown_jenjang = nested_result["breakdown_jenjang"]
            breakdown_status  = nested_result["breakdown_status"]
            logger.info(
                "NESTED AGG total=%s jenjang_keys=%s status_keys=%s",
                total,
                list(breakdown_jenjang.keys())[:5],
                list(breakdown_status.keys()),
            )

    else:
        # Kecamatan/Desa: tidak ada nested agg, langsung query ES untuk total
        total = await _run_metric_query()
        _qc_aggs_result = {}

    # ── Log result ─────────────────────────────────────────────────────────
    logger.info(f"AGG TOTAL = {total}")
    logger.info(f"AGG ENTITIES = {entities}")
    
    if total is None:
        return _error("ELASTICSEARCH_ERROR")
    
    # ── Hitung total_all untuk persentase ─────────────────────────────
    _is_numeric_metric = _metric_target and _metric_target != "sekolah"

    has_operational_filter = bool(
        query_filters.get("akreditasi_gte") or
        query_filters.get("akreditasi_exact") or
        query_filters.get("bentuk_pendidikan") or
        query_filters.get("status_sekolah")
    )

    total_all = None
    pct_of_total = None
    # Jangan hitung pct jika metric adalah numerik (siswa/guru) —
    # tidak bisa dibandingkan dengan count dokumen sebagai total_all
    if has_operational_filter and total is not None and total > 0 and not _is_numeric_metric:
        location_only = {
            k: v for k, v in normalized.items()
            if k not in (
                "bentuk_pendidikan",
                "status_sekolah",
                "akreditasi_gte",
                "akreditasi_exact",
                "_akreditasi_values"  # optional (sudah tidak dipakai sebenarnya)
            )
        }

        total_all = await execute_aggregation_query(es_client, location_only)

        if total_all and total_all > 0:
            pct_of_total = round(total / total_all * 100, 1)
            logger.info(
                "[AGG] total=%s total_all=%s pct=%.1f%%",
                total, total_all, pct_of_total
            )
    
    # ── Filter breakdown_jenjang sesuai jenjang yang user minta ────────────
    # Jika user sebut jenjang spesifik, tampilkan hanya itu
    # Jika tidak ada preferensi jenjang, tampilkan semua (default behavior)
    if not jenjang_requested:
        jenjang_requested = query_filters.get("jenjang_requested", [])

    if jenjang_requested and breakdown_jenjang:
        # Normalize ke uppercase biar robust
        requested_upper = [j.upper() for j in jenjang_requested]

        breakdown_jenjang_filtered = {
            k: v for k, v in breakdown_jenjang.items()
            if k.upper() in requested_upper
        }

        # Pakai hasil filter jika tidak kosong
        if breakdown_jenjang_filtered:
            breakdown_jenjang = breakdown_jenjang_filtered
            logger.info(
                "[AGG] breakdown filtered to requested jenjang: %s",
                list(breakdown_jenjang.keys())
            )
    # ─────────────────────────────────────────────────────────────
    if total == 0:
        result = _validation(
            "DATA_NOT_FOUND",
            query,
            resolution,
            ranking_gap,
            entities
        )
        logger.warning(f"ZERO RESULT for entities: {entities}")
        await set_cache(cache_key, result, 600)
        return result

    confidence = compute_aggregation_confidence(
        query=query,
        resolution_status = (
            resolution["status"].value
            if hasattr(resolution["status"], "value")
            else str(resolution["status"])
        ),
        resolution_metadata=resolution.get("metadata", {}),
        total_hits=total,
        ranking_gap=ranking_gap
    )

    answer_confident = confidence >= 0.7
    result = {
        "status": ResultStatus.SUCCESS,
        "provider": "ELASTICSEARCH_AGGREGATION",
        "total": total,
        "metric_target": _metric_target or "sekolah",
        "total_all": total_all,
        "pct_of_total": pct_of_total,
        "active_filters": query_filters,
        "jenjang_requested": jenjang_requested,
        "entities": entities,
        "confidence": confidence,
        "answer_confident": answer_confident,
        "metadata": resolution.get("metadata", {}),
        "breakdown_jenjang": breakdown_jenjang,
        "breakdown_status":  breakdown_status,
        "drilldown_field":   drilldown_field or "",
        "drilldown_top3":    drilldown_top3,
        "qc_aggs":           _qc_aggs_result,
    }

    await set_cache(cache_key, result, 3600)
    return result

def _cache_key(entities: Any) -> str:

    raw = json.dumps(
        entities,
        sort_keys=True,
        ensure_ascii=False
    )

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
