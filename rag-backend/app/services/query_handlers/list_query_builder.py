"""
Elasticsearch query builder untuk list school handler.
Dipindah dari list_school_handler.py (P17-A refactor).
"""
import re
import logging
from typing import Dict, List, Optional

from app.services.core.es_query_builder import build_qc_filter, build_numeric_filters, build_boolean_filters
from app.services.core.jenjang_constants import JENJANG_MAP
from app.services.query_handlers.list_sort_utils import detect_sort_mode, build_sort_clause

logger = logging.getLogger(__name__)

# PAUD adalah kategori umbrella — expand ke jenjang konkret di ES
_PAUD_BENTUK_EXPAND = ["TK", "KB", "RA", "SPS", "TPA", "PAUDQ", "SPK TK", "SPK KB", "SDTK"]

LIST_AGGS_SNIPPET = {
    "aggs": {
        "by_jenjang": {"terms": {"field": "bentuk_pendidikan", "size": 20}},
        "by_status":  {"terms": {"field": "status_sekolah", "size": 5}},
    }
}


def extract_breakdowns(es_response: dict):
    agg = es_response.get("aggregations", {})
    jenjang = {
        b["key"]: b["doc_count"]
        for b in agg.get("by_jenjang", {}).get("buckets", [])
    }
    status = {
        b["key"]: b["doc_count"]
        for b in agg.get("by_status", {}).get("buckets", [])
    }
    return jenjang, status


def hybridize_query(query_body: Dict, qv: List[float]) -> None:
    """In-place: wrap existing bool query with function_score for vector similarity boost."""
    existing = query_body.get("query")
    if not existing:
        return
    query_body["query"] = {
        "function_score": {
            "query": existing,
            "functions": [{
                "script_score": {
                    "script": {
                        "source": (
                            "doc['content_vector'].size() > 0 "
                            "? cosineSimilarity(params.qv, 'content_vector') + 1.0 "
                            ": 1.0"
                        ),
                        "params": {"qv": qv},
                    }
                }
            }],
            "boost_mode": "multiply",
        }
    }
    logger.info("[HYBRID] query injected, vector_dims=%d", len(qv))


def match_phrase(field: str, value: str) -> Dict:
    return {"match_phrase": {field: value}}


def match_phrase_boosted(field: str, value: str, boost: float) -> Dict:
    return {"bool": {"should": [{"match_phrase": {field: value}}],
                    "minimum_should_match": 1, "boost": boost}}


def strip_prefix(text: str) -> str:
    return re.sub(
        r"^(kota\s+adm\.?\s*|kota\s+|kab\.?\s+|kabupaten\s+|"
        r"prov\.?\s+|provinsi\s+|kec\.?\s+|kecamatan\s+|"
        r"desa\s+|kelurahan\s+)",
        "", text
    ).strip()


def build_wilayah_filters_and_should(
    parsed: Dict,
    wilayah_level: str,
    query_raw: str = "",
) -> tuple:
    """
    Build (filters, should) list berdasarkan wilayah dan level.
    Return: (filters: List, should: List)
    """
    # Pulau multi-province filter takes precedence over single-wilayah logic
    provinsi_list = parsed.get("provinsi_list")
    if provinsi_list:
        return [{"terms": {"provinsi": provinsi_list}}], []

    wilayah = parsed.get("wilayah")
    filters = []
    should = []

    if not wilayah:
        return filters, should

    wc = strip_prefix(wilayah.lower())
    q_check = query_raw.lower() if query_raw else ""
    want_kota = bool(re.search(r"\bkota\b", q_check)) and not bool(re.search(r"\b(kabupaten|kab)\b", q_check))
    want_kab = bool(re.search(r"\b(kabupaten|kab)\b", q_check)) and not bool(re.search(r"\bkota\b", q_check))

    logger.info("[LIST] _build_query: wc=%s want_kab=%s want_kota=%s q=%s",
                wc, want_kab, want_kota, q_check[:60])

    _cur_wilayah = parsed.get("wilayah", "").lower()
    if want_kab and not _cur_wilayah.startswith("kab"):
        parsed["wilayah"] = f"kabupaten {wc}"
    elif want_kota and not _cur_wilayah.startswith("kota"):
        parsed["wilayah"] = f"kota {wc}"

    if wilayah_level == "provinsi":
        should += [
            {"match": {"provinsi.text": {"query": wc, "boost": 20}}},
            {"term":  {"provinsi": {"value": wc, "boost": 15}}},
            {"term":  {"provinsi": {"value": f"prov. {wc}", "boost": 15}}},
        ]
        filters.append({"bool": {"should": [
            {"match": {"provinsi.text": wc}},
            {"term":  {"provinsi": wc}},
            {"term":  {"provinsi": f"prov. {wc}"}},
        ], "minimum_should_match": 1}})

    elif wilayah_level == "desa_kelurahan":
        should += [
            {"term":  {"desa_kelurahan": {"value": wc, "boost": 20}}},
            match_phrase("desa_kelurahan.text", wc),
            match_phrase("kecamatan.text", wc),
        ]
        filters.append({"bool": {"should": [
            {"term":  {"desa_kelurahan": wc}},
            match_phrase("desa_kelurahan.text", wc),
        ], "minimum_should_match": 1}})

    elif wilayah_level == "kecamatan":
        q_has_kec_hint = bool(re.search(r"\b(kecamatan|kec\.?)\b", q_check))
        if q_has_kec_hint:
            should += [
                {"term":  {"kecamatan": {"value": wc, "boost": 25}}},
                {"term":  {"kecamatan": {"value": f"kec. {wc}", "boost": 25}}},
                match_phrase_boosted("kecamatan.text", wc, boost=25),
            ]
            filters.append({"bool": {"should": [
                {"term":  {"kecamatan": wc}},
                {"term":  {"kecamatan": f"kec. {wc}"}},
                match_phrase("kecamatan.text", wc),
            ], "minimum_should_match": 1}})
        else:
            should += [
                {"term":  {"kecamatan": {"value": wc, "boost": 25}}},
                {"term":  {"kecamatan": {"value": f"kec. {wc}", "boost": 25}}},
                match_phrase_boosted("kecamatan.text", wc, boost=25),
                {"term":  {"kabupaten": {"value": wc, "boost": 10}}},
                {"term":  {"kabupaten": {"value": f"kab. {wc}", "boost": 10}}},
                {"term":  {"kabupaten": {"value": f"kota {wc}", "boost": 10}}},
                match_phrase("kabupaten.text", wc),
                {"match": {"provinsi.text": {"query": wc, "boost": 3}}},
            ]
            filters.append({"bool": {"should": [
                {"term":  {"kecamatan": wc}},
                {"term":  {"kecamatan": f"kec. {wc}"}},
                match_phrase("kecamatan.text", wc),
                {"term":  {"kabupaten": wc}},
                {"term":  {"kabupaten": f"kab. {wc}"}},
                {"term":  {"kabupaten": f"kota {wc}"}},
                match_phrase("kabupaten.text", wc),
            ], "minimum_should_match": 1}})

    else:  # kabupaten (default)
        if want_kab:
            should += [
                {"term": {"kabupaten": {"value": f"kab. {wc}", "boost": 30}}},
                {"term": {"kabupaten": {"value": wc, "boost": 5}}},
                match_phrase("kecamatan.text", wc),
                {"match": {"provinsi.text": {"query": wc, "boost": 2}}},
            ]
            filters.append({"bool": {"should": [
                {"term": {"kabupaten": f"kab. {wc}"}},
                {"term": {"kabupaten": wc}},
            ], "minimum_should_match": 1}})
        elif want_kota:
            should += [
                {"term": {"kabupaten": {"value": f"kota {wc}", "boost": 30}}},
                {"term": {"kabupaten": {"value": f"kab. kota {wc}", "boost": 20}}},
                match_phrase("kecamatan.text", wc),
                {"match": {"provinsi.text": {"query": wc, "boost": 2}}},
            ]
            filters.append({"bool": {"should": [
                {"term": {"kabupaten": f"kota {wc}"}},
                {"term": {"kabupaten": f"kab. kota {wc}"}},
                match_phrase("kabupaten.text", wc),
                match_phrase("kecamatan.text", wc),
            ], "minimum_should_match": 1}})
        else:
            should += [
                {"term": {"kabupaten": {"value": wc, "boost": 20}}},
                {"term": {"kabupaten": {"value": f"kab. {wc}", "boost": 20}}},
                {"term": {"kabupaten": {"value": f"kota {wc}", "boost": 20}}},
                {"term": {"kabupaten": {"value": f"kab. kota {wc}", "boost": 20}}},
                match_phrase("kabupaten.text", wc),
                match_phrase("kecamatan.text", wc),
                {"match": {"provinsi.text": {"query": wc, "boost": 3}}},
            ]
            filters.append({"bool": {"should": [
                {"term": {"kabupaten": wc}},
                {"term": {"kabupaten": f"kab. {wc}"}},
                {"term": {"kabupaten": f"kota {wc}"}},
                {"term": {"kabupaten": f"kab. kota {wc}"}},
                match_phrase("kabupaten.text", wc),
                match_phrase("kecamatan.text", wc),
            ], "minimum_should_match": 1}})

    return filters, should


def build_base_filters(parsed: Dict, query_raw: str = "", intent=None) -> List:
    """Build filter list untuk bentuk, status, akreditasi, dan QC."""
    bentuk = parsed.get("bentuk")
    bentuk_in = parsed.get("bentuk_pendidikan_in")
    status = parsed.get("status")
    akreditasi = parsed.get("akreditasi")
    akreditasi_in = parsed.get("akreditasi_in")
    akreditasi_gte = parsed.get("akreditasi_gte")
    akreditasi_exc = parsed.get("akreditasi_exclude")

    if not status:
        sn = (parsed.get("school_name") or "").lower()
        if re.search(r"\bnegeri\b", sn) or re.search(r"\b n \b", sn):
            status = "NEGERI"

    if query_raw:
        _qr = query_raw.lower()
        _multi_jenjang = re.findall(
            r'\b(spk\s+(?:sd|smp|sma|smk|tk|kb)|sd|smp|sma|smk|ma|mts|mi|tk|paud|kb|ra|sps|tpa)\b', _qr
        )
        _seen = set()
        _multi_jenjang = [x for x in _multi_jenjang if not (_seen.add(x) or x in _seen)]
        if len(_multi_jenjang) >= 2:
            _jenjang_es = []
            for j in _multi_jenjang:
                if j == "paud":
                    _jenjang_es.extend(_PAUD_BENTUK_EXPAND)
                else:
                    _jenjang_es.append(JENJANG_MAP.get(j, j.upper()))
            bentuk_in = list(dict.fromkeys(_jenjang_es))
            bentuk = None

    filters = []
    if bentuk_in:
        filters.append({"terms": {"bentuk_pendidikan": bentuk_in}})
    elif bentuk == "PAUD":
        filters.append({"terms": {"bentuk_pendidikan": _PAUD_BENTUK_EXPAND}})
    elif bentuk:
        filters.append({"term": {"bentuk_pendidikan": bentuk}})

    if status:
        filters.append({"term": {"status_sekolah": status.upper()}})

    if akreditasi_in:
        filters.append({"terms": {"akreditasi": akreditasi_in}})
        logger.info("[LIST] akreditasi_in filter: %s", akreditasi_in)
    elif akreditasi_gte:
        filters.append({"terms": {"akreditasi": akreditasi_gte}})
        logger.info("[LIST] akreditasi_gte filter: %s", akreditasi_gte)
    elif akreditasi_exc:
        filters.append({"bool": {"must_not": [{"term": {"akreditasi": akreditasi_exc}}]}})
        logger.info("[LIST] akreditasi_exclude filter: %s", akreditasi_exc)
    elif akreditasi:
        filters.append({"term": {"akreditasi": akreditasi}})
        logger.info("[LIST] akreditasi filter: %s", akreditasi)

    if intent is not None:
        qc_filters = build_qc_filter(intent)
        if qc_filters:
            filters.extend(qc_filters)
            logger.info("[LIST] QC filters injected: %s", qc_filters)

    numeric_filters = parsed.get("numeric_filters") or []
    if numeric_filters:
        nf_clauses = build_numeric_filters(numeric_filters)
        if nf_clauses:
            filters.extend(nf_clauses)
            logger.info("[LIST] numeric_filters applied: %d clause(s)", len(nf_clauses))

    boolean_clauses = build_boolean_filters(parsed)
    if boolean_clauses:
        filters.extend(boolean_clauses)
        logger.info("[LIST] boolean filters applied: %d", len(boolean_clauses))

    return filters


def build_list_query(
    parsed: Dict,
    wilayah_level: str = "kabupaten",
    query_raw: str = "",
    intent=None,
) -> Dict:
    """Build lengkap ES query body untuk list school (single wilayah)."""
    filters = build_base_filters(parsed, query_raw=query_raw, intent=intent)
    wil_filters, should = build_wilayah_filters_and_should(parsed, wilayah_level, query_raw)
    filters.extend(wil_filters)

    sort_mode = detect_sort_mode(query_raw, intent=intent)
    query_body = {
        "size": 10,
        "track_total_hits": True,
        "query": {"bool": {
            "filter": filters,
            "should": should,
            **( {"minimum_should_match": 1} if should else {}),
        }},
        "collapse": {"field": "npsn"},
        "sort": build_sort_clause(sort_mode, intent=intent),
    }
    logger.debug("[LIST] build_list_query (pre-hybrid): %s", query_body)
    return query_body
