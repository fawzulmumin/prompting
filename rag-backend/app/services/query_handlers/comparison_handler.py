# comparison_handler.py
 
import logging
import re
import os
import asyncio as _asyncio
from typing import Dict, Any, Optional, List, Tuple
from elasticsearch import AsyncElasticsearch
 
from app.services.core.result_model import ResultStatus
from app.services.entity_service import (
    resolve_entities,
    validate_hierarchy_integrity
)
from app.services.core.aggregation_core import (
    execute_aggregation_query,
    normalize_entity_hierarchy,
    execute_nested_aggregation_query,
)
from app.services.query_handlers import profile_handler
from app.services.core.es_query_builder import extract_filters_from_intent
from app.services.confidence_engine import compute_comparison_confidence
from app.utils.location_utils import fuzzy_match_location, fuzzy_correct_words
 
logger = logging.getLogger(__name__)
 

def _build_school_vs_school_response(school_a, school_b, query):
    return {
        "status": ResultStatus.SUCCESS,
        "provider": "PROFILE_COMPARISON",
        "type": "school_vs_school",
        "query": query,
        "data": {
            "school_a": school_a,
            "school_b": school_b,
        },
        "answer_confident": True,
        "confidence": 0.95
    }

def get_display_value(entities: Dict[str, str]) -> str:
    if "desa_kelurahan" in entities and "kecamatan" in entities:
        desa = entities["desa_kelurahan"]
        kec = entities["kecamatan"].replace("kec. ", "")
        return f"{desa} ({kec})"
    for level in ["desa_kelurahan", "kecamatan", "kabupaten", "provinsi"]:
        if level in entities:
            return entities[level]
    return list(entities.values())[0]
 
 
# =====================================================
# FORMAT LOCATION CONSISTENT
# =====================================================
def format_location(value: str) -> str:
    v = value.lower().strip()
    if v.startswith("prov."):
        return "Prov. " + v.replace("prov.", "").strip().title()
    if v.startswith("kab."):
        return "Kab. " + v.replace("kab.", "").strip().title()
    if v.startswith("kota"):
        return "Kota " + v.replace("kota", "").strip().title()
    if v.startswith("kec."):
        return "Kec. " + v.replace("kec.", "").strip().title()
    return v.title()
 
 
# =====================================================
# STATUS SEKOLAH DETECTION
# =====================================================
 
# Mapping keyword → nilai status_sekolah di ES
STATUS_KEYWORD_MAP = {
    "negeri":  "Negeri",
    "swasta":  "Swasta",
    "n":       "Negeri",   # singkatan tidak umum tapi tetap disiapkan
}
 
def detect_status_sekolah(text: str) -> Optional[str]:
    """
    Ekstrak status_sekolah dari teks query.
    Return "Negeri" / "Swasta" / None.
    """
    t = text.lower()
    for kw, val in STATUS_KEYWORD_MAP.items():
        # pastikan kata utuh (tidak menempel ke kata lain)
        if re.search(rf"\b{re.escape(kw)}\b", t):
            return val
    return None
 
 
# =====================================================
# COMPARISON KEYWORDS
# =====================================================
 
# Separator untuk memisahkan dua sisi lokasi
VS_SEPARATORS = [" vs ", " versus ", " dan ", " with ", " dengan ", ", "]
 
COMPARISON_KEYWORDS = (
    "vs", "versus", "bandingkan", "dibandingkan",
    "perbandingan", "lebih banyak", "lebih sedikit",
    "mana yang lebih", "mana lebih", "dibanding"
)
 
 
def is_comparison_query(query: str) -> bool:
    q = query.lower()
    
    # GUARD: jika hanya ada satu sinyal lokasi (kabupaten/kota tunggal)
    # dan ada kata aggregation → ini bukan comparison
    _AGG_SIGNALS = {"berapa", "jumlah", "total", "banyak", "persentase"}
    _LOC_HINTS = ["kabupaten", "kab.", "kota", "provinsi", "kecamatan"]
    
    has_agg_signal = any(w in q for w in _AGG_SIGNALS)
    loc_count = sum(1 for h in _LOC_HINTS if h in q)
    
    # Satu lokasi + sinyal aggregation = bukan comparison
    if has_agg_signal and loc_count == 1:
        return False
    
    # GUARD: jika query adalah perbandingan dua nama sekolah spesifik
    # (mengandung nama sekolah + angka di kedua sisi vs)
    # → bukan comparison lokasi, arahkan ke profile handler
    _SCHOOL_VS_RE = re.compile(
        r'\b(sd|mi|smp|mts|sma|smk|ma|sman|smpn|sdn|smkn|man|mtsn|min)'
        r'\s*(?:negeri|swasta)?\s*\d'
        r'.*?\bvs\b.*?'
        r'\b(sd|mi|smp|mts|sma|smk|ma|sman|smpn|sdn|smkn|man|mtsn|min)'
        r'\s*(?:negeri|swasta)?\s*\d',
        re.IGNORECASE | re.DOTALL
    )
    if _SCHOOL_VS_RE.search(q):
        return False  # biarkan profile handler yang tangani
    
    has_comparison_keyword = any(k in q for k in COMPARISON_KEYWORDS)
 
    has_location_comma = False
    if "," in q and not has_comparison_keyword:
        HIERARCHY_WORDS = (
            "jawa", "sumatera", "kalimantan", "sulawesi",
            "bali", "prov.", "kab.", "kota", "kec.",
            "provinsi", "kabupaten", "kecamatan", "desa"
        )
        parts = [p.strip() for p in q.split(",")]
        non_hierarchy_parts = [
            p for p in parts[1:]
            if not any(hw in p for hw in HIERARCHY_WORDS)
        ]
        if len(non_hierarchy_parts) >= 1 and len(parts) >= 2:
            has_location_comma = True
 
    # Pattern implisit: "sekolah X dan Y" atau "sekolah X dengan Y"
    import re as _re        
    _implicit = bool(_re.search(
        r'\bsekolah\b[\w\s]{2,40}\b(dan|dengan)\b[\w\s]{2,30}',
        q, _re.IGNORECASE
    ))
 
    _JENJANG = r"(sd|smp|sma|smk|tk|mi|mts|ma|paud|kb|ra|sps|tpa|negeri|swasta)"
    _jenjang_only = bool(_re.search(
        r"^(bandingkan\s+)?(sekolah\s+)?" + _JENJANG +
        r"(\s*[,/]\s*" + _JENJANG + r")*"
        r"(\s+(dan|atau|serta|dengan)\s+" + _JENJANG + r")+"
        r"\s+(di|dalam)\b",
        q, _re.IGNORECASE
    ))
    if _jenjang_only:
        _implicit = False
        has_location_comma = False
        has_comparison_keyword = False
 
    return has_comparison_keyword or has_location_comma or _implicit
# =========================
# GLOBAL REGEX / CONSTANT
# =========================
_SCHOOL_NAME_PATTERN = re.compile(
    r'\b(sd|mi|smp|mts|sma|smk|ma|sman|smpn|sdn|smkn|man|mtsn|min)\s*n?\s*\d',
    re.IGNORECASE
)

# =========================
# HELPER FUNCTION (GLOBAL)
# =========================
def _is_school_vs_school(left: str, right: str) -> bool:
    return bool(
        _SCHOOL_NAME_PATTERN.search(left) and
        _SCHOOL_NAME_PATTERN.search(right)
    )

def _auto_disambiguate_school_pair(left_candidates, right_candidates):
    left_kabs = {c["kabupaten"] for c in left_candidates}
    right_kabs = {c["kabupaten"] for c in right_candidates}
    
    shared_kabs = left_kabs & right_kabs
    
    if not shared_kabs:
        return None, None
    
    def best_candidate(candidates, target_kab):
        matches = [c for c in candidates if c["kabupaten"] == target_kab]
        return max(matches, key=lambda x: x.get("doc_count", 0))
    
    best_kab = max(shared_kabs, key=lambda k: sum(
        c.get("doc_count", 0) for c in left_candidates if c["kabupaten"] == k
    ))
    
    return best_candidate(left_candidates, best_kab), best_candidate(right_candidates, best_kab)

def _format_disambiguation_message(school_name: str, candidates: list) -> str:
    lines = [f"Ditemukan **'{school_name}'** di beberapa lokasi:\n"]
    for c in candidates[:3]:
        kec = c.get("kecamatan", "").replace("Kec. ", "")
        kab = c.get("kabupaten", "")
        lines.append(f"- **{kab}** (Kec. {kec})")
    lines.append(f"\nSilakan tambahkan nama kabupaten/kota, contoh:")
    lines.append(f'- "{school_name}, Kota Bandung"')
    lines.append(f'- "{school_name}, Kab. Bandung"')
    return "\n".join(lines)

def graceful_error_response() -> Dict[str, Any]:
    return {
        "answer_markdown": "Terjadi kesalahan dalam memproses perbandingan lokasi.",
        "provider": "SYSTEM_ERROR",
        "answer_confident": False
    }
 
 
def _build_comparison_agg_query(entities: dict, jenjang: str = None) -> dict:
    filters = []

    for field in ["provinsi", "kabupaten", "kecamatan", "desa_kelurahan"]:
        if entities.get(field):
            filters.append({"term": {field: entities[field]}})

    if jenjang:
        filters.append({"term": {"bentuk_pendidikan": jenjang}})

    # pakai npsn langsung (karena sudah keyword)
    count_field = "npsn"

    return {
        "size": 0,
        "query": {"bool": {"filter": filters}},
        "aggs": {
            "total_sekolah": {"value_count": {"field": count_field}},
        }
    }
# =====================================================
# MAIN HANDLER
# =====================================================

def _extract_wilayah_from_intent(intent) -> dict:
    """
    Extract wilayah_1, wilayah_2, scope from intent for comparison short-circuit.
    Returns {"wilayah_1": str|None, "wilayah_2": str|None, "scope": str|None}.
    """
    result = {"wilayah_1": None, "wilayah_2": None, "scope": None}
    if intent is None:
        return result
    w = getattr(intent, "wilayah", None)
    if w is None:
        return result
    if getattr(w, "kecamatan", None):
        result["wilayah_1"] = w.kecamatan.lower().strip()
        result["scope"] = "kecamatan"
    elif getattr(w, "kabupaten", None):
        result["wilayah_1"] = w.kabupaten.lower().strip()
        result["scope"] = "kabupaten"
    elif getattr(w, "provinsi", None):
        result["wilayah_1"] = w.provinsi.lower().strip()
        result["scope"] = "provinsi"
    w2 = getattr(intent, "wilayah_bandingkan", None)
    if w2:
        result["wilayah_2"] = w2.lower().strip()
    return result


async def handle_comparison_query(
    query: str,
    es_client: AsyncElasticsearch,
    intent=None,
    filters: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    # ── EARLY DETECTION: school vs school ─────────────────────
    q_lower = query.lower()

    sep_found = None
    for sep in VS_SEPARATORS:
        if sep in q_lower:
            sep_found = sep
            break

    if sep_found:
        parts = q_lower.split(sep_found, 1)
        if len(parts) == 2:
            left_text = parts[0].strip()
            right_text = parts[1].strip()

            if _is_school_vs_school(left_text, right_text):
                logger.info("[COMPARISON] Detected school vs school → redirect to profile handler")

                # Jalankan dua profile search secara paralel
                res_a, res_b = await _asyncio.gather(
                    profile_handler.search_by_name(left_text),
                    profile_handler.search_by_name(right_text),
                )

                # ── HANDLE AMBIGUITY AUTO RESOLVE ─────────────────────
                if (
                    isinstance(res_a, dict) and res_a.get("status") == ResultStatus.AMBIGUOUS and
                    isinstance(res_b, dict) and res_b.get("status") == ResultStatus.AMBIGUOUS
                ):
                    best_a, best_b = _auto_disambiguate_school_pair(
                        res_a.get("data", {}).get("candidates", []),
                        res_b.get("data", {}).get("candidates", [])
                    )

                    if best_a and best_b:
                        logger.info("[COMPARISON] Auto-disambiguated school pair using shared kabupaten")

                        school_a = best_a
                        school_b = best_b

                        return _build_school_vs_school_response(
                            school_a,
                            school_b,
                            query
                        )

                # ── FALLBACK: kalau salah satu ambiguous ───────────────
                if isinstance(res_a, dict) and res_a.get("status") == ResultStatus.AMBIGUOUS:
                    return res_a

                if isinstance(res_b, dict) and res_b.get("status") == ResultStatus.AMBIGUOUS:
                    return res_b

                # ── NORMAL CASE ───────────────────────────────────────
                school_a = res_a
                school_b = res_b

                return _build_school_vs_school_response(
                    school_a,
                    school_b,
                    query
                )
                
    # ── INTENT SHORT-CIRCUIT: trust classifier when both locations known ──────
    _iw = _extract_wilayah_from_intent(intent)
    _intent_has_both = bool(_iw["wilayah_1"] and _iw["wilayah_2"] and _iw["scope"])

    # ── FAST PATH: VS query → split & resolve independen ─────
    vs_result = None if _intent_has_both else await _resolve_vs_query(query, es_client)
 
    # ── Cek ambiguous ─────────────────────────────────────────
    if isinstance(vs_result, dict) and vs_result.get("ambiguous"):
        ambiguous_part = vs_result["ambiguous_part"]
        candidates = vs_result["candidates"]

        # PRIORITAS: pakai message dari resolver kalau ada
        message = vs_result.get("message")

        # FALLBACK: pakai helper standard
        if not message:
            message = _format_disambiguation_message(ambiguous_part, candidates)

        return {
            "status": ResultStatus.AMBIGUOUS,
            "provider": "SYSTEM_AMBIGUOUS",
            "data": {
                "candidates": candidates,
                "message": message,
            },
            "confidence_score": 0.0,
            "answer_confident": False
        }
    
    # ── Status comparison (negeri vs swasta dalam satu wilayah) ──
    if isinstance(vs_result, dict) and vs_result.get("status_comparison"):
        return await _handle_status_comparison(
            query=query,
            es_client=es_client,
            entities=vs_result["entities"],
            statuses=vs_result["statuses"],
        )
 
    # ── Jenjang comparison (sma vs smk dalam satu wilayah) ────────
    if isinstance(vs_result, dict) and vs_result.get("jenjang_comparison"):
        return await _handle_jenjang_comparison(
            query=query,
            es_client=es_client,
            entities=vs_result["entities"],
            jenjang_list=vs_result["jenjang_list"],
        )
 
    if _intent_has_both:
        logger.info(
            "[COMPARISON] intent.wilayah trusted: w1=%s w2=%s scope=%s → skip resolver",
            _iw["wilayah_1"], _iw["wilayah_2"], _iw["scope"],
        )
        locations = [{_iw["scope"]: _iw["wilayah_1"]}, {_iw["scope"]: _iw["wilayah_2"]}]
    elif vs_result and isinstance(vs_result, list) and len(vs_result) >= 2:
        locations = vs_result
    else:
        # ── SLOW PATH: resolve normal (untuk koma, dll) ───────
        resolved = await resolve_entities(query, es_client)
 
        if resolved["status"] == ResultStatus.AMBIGUOUS:
            resolved["confidence_score"] = 0.0
            resolved["answer_confident"] = False
            return resolved
 
        if resolved["status"] == ResultStatus.VALIDATION_ERROR:
            return resolved
 
        if resolved["status"] != ResultStatus.SUCCESS:
            return None
 
        locations = []
        data = resolved.get("data", {})
 
        if data.get("is_multi"):
            for ent in data.get("entities", []):
                if isinstance(ent, dict) and ent:
                    locations.append(ent)
        else:
            single = data.get("entities", {})
            if isinstance(single, dict) and single:
                locations.append(single)
 
        # DEDUPLICATE
        unique = {}
        for loc in locations:
            key = tuple(sorted(loc.items()))
            unique[key] = loc
        locations = list(unique.values())
 
    # ── Harus minimal 2 lokasi ────────────────────────────────
    if len(locations) < 2:
        return {
            "provider": "SYSTEM_VALIDATION",
            "reason": "COMPARISON_NEEDS_MULTIPLE_LOCATIONS",
            "answer_confident": False
        }
 
    # ── Execute aggregation per lokasi ────────────────────────
    results: List[Dict[str, Any]] = []
 
    async def _fetch_location_data(loc_data: dict) -> dict:

        entities = dict(loc_data)
        normalized = normalize_entity_hierarchy(entities)

        # Ambil filter SEKALI saja
        qf = filters or extract_filters_from_intent(intent)

        if qf.get("bentuk_pendidikan"):
            normalized["bentuk_pendidikan"] = qf["bentuk_pendidikan"]

        if qf.get("status_sekolah"):
            normalized["status_sekolah"] = qf["status_sekolah"]

        if qf.get("akreditasi_gte"):
            normalized["akreditasi_gte"] = qf["akreditasi_gte"]

        if qf.get("akreditasi_in"):
            normalized["akreditasi_in"] = qf["akreditasi_in"]

        if qf.get("bentuk_pendidikan_in"):
            normalized["bentuk_pendidikan_in"] = qf["bentuk_pendidikan_in"]

        if qf.get("penyelenggara"):
            normalized["penyelenggara"] = qf["penyelenggara"]
        elif qf.get("akreditasi_exact"):
            normalized["akreditasi_exact"] = qf["akreditasi_exact"]

        # Ambil jenjang untuk avg comparison
        jenjang = qf.get("bentuk_pendidikan")

        agg_query = _build_comparison_agg_query(entities, jenjang)

        es_result = await es_client.search(
            index=os.getenv("ELASTICSEARCH_INDEX", "idx_sekolah_qc"),
            body=agg_query
        )

        aggs = es_result.get("aggregations", {})

        total_sekolah = aggs.get("total_sekolah", {}).get("value", 0)

        display_value = get_display_value(entities)

        return {
            "value": display_value,
            "count": total_sekolah,
            "entities": entities,
        }
 
    for loc_data in locations:
        if not isinstance(loc_data, dict):
            logger.warning("[COMPARISON] loc_data invalid type: %s", type(loc_data))
            return graceful_error_response()
 
        hierarchy_check = validate_hierarchy_integrity(loc_data)
        if hierarchy_check.get("status") == ResultStatus.VALIDATION_ERROR:
            return {
                "provider": "SYSTEM_VALIDATION",
                "reason": "HIERARCHY_INCOMPLETE"
            }
 
        try:
            result = await _fetch_location_data(loc_data)
            results.append(result)
        except Exception:
            logger.exception("Comparison aggregation failed for loc: %s", loc_data)
            results.append({
                "value":             get_display_value(loc_data),
                "count":             0,
                "entities":          loc_data,
                "breakdown_jenjang": {},
                "breakdown_status":  {},
            })
 
    # ── Build Markdown Output ─────────────────────────────────
    if not results:
        return None
 
    results.sort(key=lambda x: x["count"], reverse=True)
 
    # Ambil jenjang dari filter (jika ada)
    qf = filters or extract_filters_from_intent(intent)
    jenjang = qf.get("bentuk_pendidikan")

    # Optional: label biar lebih rapi (SMA, SMK, dll)
    jenjang_label = jenjang.upper() if jenjang else None

    # Header dinamis
    if jenjang_label:
        markdown = f"### Perbandingan Sekolah {jenjang_label}\n\n"
    else:
        markdown = "### Perbandingan Jumlah Sekolah\n\n"
    for r in results:
        label = format_location(r["value"])
        markdown += f"- {label}: **{r['count']:,} sekolah**\n"
 
    best = results[0]
    best_label = format_location(best["value"])
    markdown += f"\n---\n"
    markdown += f"Wilayah dengan sekolah terbanyak adalah **{best_label}**."
 
    total_hits = sum(r["count"] for r in results)
 
    confidence = compute_comparison_confidence(
        entities_count=len(results),
        total_hits=total_hits
    )
 
    return {
        "query": query,
        "answer_markdown": markdown,
        "provider": "ELASTICSEARCH_COMPARISON",
        "results": [(r["value"], r["count"]) for r in results],
        "results_detail": results,
        "context_used": results,
        "answer_confident": confidence >= 0.7,
        "confidence": confidence,
        "num_docs": total_hits
    }
 
 
# =====================================================
# STATUS COMPARISON (negeri vs swasta dalam 1 wilayah)
# =====================================================
 
async def _handle_status_comparison(
    query: str,
    es_client: AsyncElasticsearch,
    entities,          # bisa dict ATAU list[dict] jika AREA_GROUP
    statuses: List[str],
) -> Dict[str, Any]:
    """
    Bandingkan jumlah sekolah Negeri vs Swasta.
    Mendukung entities tunggal (dict) maupun multi (list of dict dari AREA_GROUP).
    """
    # Normalisasi ke list agar loop seragam
    if isinstance(entities, dict):
        entities_list = [entities]
        area_label    = None
    else:
        entities_list = entities
        area_label    = None  # bisa diambil dari metadata jika perlu
 
    results: List[Dict[str, Any]] = []
 
    for status_val in statuses:
        total_for_status = 0
 
        for ent in entities_list:
            # Validasi hierarchy per entitas
            hierarchy_check = validate_hierarchy_integrity(ent)
            if hierarchy_check.get("status") == ResultStatus.VALIDATION_ERROR:
                continue  # skip entitas yang tidak valid, bukan gagal total
 
            ent_with_status = {**ent, "status_sekolah": status_val}
            try:
                normalized = normalize_entity_hierarchy(ent_with_status)
                total      = await execute_aggregation_query(es_client, normalized)
                total_for_status += total or 0
            except Exception:
                logger.exception(
                    "Status comparison aggregation failed for %s / %s",
                    status_val, ent
                )
 
        results.append({
            "status": status_val,
            "count":  total_for_status,
        })
 
    results.sort(key=lambda x: x["count"], reverse=True)
 
    # Label wilayah: pakai area_label jika tersedia, fallback ke entities[0]
    if area_label:
        wilayah_label = area_label
    elif isinstance(entities, list) and entities:
        wilayah_label = format_location(get_display_value(entities[0]))
    else:
        wilayah_label = format_location(get_display_value(entities))
 
    markdown  = f"### Perbandingan Sekolah Negeri vs Swasta\n"
    markdown += f"**Wilayah: {wilayah_label}**\n\n"
 
    for r in results:
        markdown += f"- Sekolah **{r['status']}**: **{r['count']:,} sekolah**\n"
 
    markdown += f"\n---\n"
 
    if results[0]["count"] == results[1]["count"]:
        markdown += "Jumlah sekolah negeri dan swasta **sama**."
    else:
        pct_diff = 0
        if results[1]["count"] > 0:
            pct_diff = round(
                (results[0]["count"] - results[1]["count"])
                / results[1]["count"] * 100
            )
        markdown += (
            f"Sekolah **{results[0]['status']}** lebih banyak "
            f"({pct_diff:+d}% dibanding {results[1]['status'].lower()})."
        )
 
    total_hits = sum(r["count"] for r in results)
    confidence = compute_comparison_confidence(
        entities_count=len(statuses),
        total_hits=total_hits
    )
 
    return {
        "query":           query,
        "answer_markdown": markdown,
        "provider":        "ELASTICSEARCH_COMPARISON_STATUS",
        "results":         [(r["status"], r["count"]) for r in results],
        "context_used":    results,
        "answer_confident": confidence >= 0.7,
        "confidence":      confidence,
        "num_docs":        total_hits
    }
 
 
# =====================================================
# JENJANG COMPARISON (sma vs smk dalam satu wilayah)
# =====================================================
 
async def _handle_jenjang_comparison(
    query: str,
    es_client: AsyncElasticsearch,
    entities: Dict[str, Any],
    jenjang_list: List[str],
) -> Dict[str, Any]:
    """
    Hitung dan bandingkan jumlah sekolah per jenjang di satu wilayah.
    Contoh: "bandingkan sma dan smk di kota bandung"
    """
    normalized_base = normalize_entity_hierarchy(dict(entities))
 
    async def _count_jenjang(jenjang: str) -> int:
        ent = dict(normalized_base)
        ent["bentuk_pendidikan"] = jenjang
        result = await execute_aggregation_query(es_client, ent)
        return result or 0
 
    counts = await _asyncio.gather(*[_count_jenjang(j) for j in jenjang_list])
 
    results = [
        {"jenjang": j, "count": c}
        for j, c in zip(jenjang_list, counts)
    ]
    results.sort(key=lambda x: x["count"], reverse=True)
 
    # ── Bangun wilayah label ──────────────────────────────────
    from app.services.formatters.response_formatter import build_location_label
    wilayah = build_location_label(entities)
 
    # ── Bangun markdown ───────────────────────────────────────
    markdown = f"### Perbandingan Jumlah Sekolah per Jenjang di {wilayah.title()}\n\n"
    markdown += "| Jenjang | Jumlah |\n|---------|--------|\n"
    for r in results:
        markdown += f"| **{r['jenjang']}** | {r['count']:,} |\n"
 
    total_hits = sum(r["count"] for r in results)
 
    if results[0]["count"] > 0:
        best = results[0]
        markdown += f"\n---\nJenjang **{best['jenjang']}** memiliki jumlah sekolah terbanyak di {wilayah.title()} dengan **{best['count']:,} sekolah**."
 
    confidence = compute_comparison_confidence(
        entities_count=len(results),
        total_hits=total_hits,
    )
 
    return {
        "query":           query,
        "answer_markdown": markdown,
        "provider":        "ELASTICSEARCH_COMPARISON_JENJANG",
        "results":         [(r["jenjang"], r["count"]) for r in results],
        "context_used":    results,
        "answer_confident": confidence >= 0.7,
        "confidence":      confidence,
        "num_docs":        total_hits,
    }
 
 
# =====================================================
# RESOLVE VS QUERY
# =====================================================
 
def _clean_location_part(p: str) -> str:
    """
    Bersihkan bagian lokasi dari kata non-lokasi.
    CATATAN: 'negeri' dan 'swasta' TIDAK dibuang di sini —
    keduanya dideteksi lebih dulu di _resolve_vs_query.
    """
    NON_LOCATION_WORDS = {
        "jumlah", "berapa", "total", "sekolah", "di", "daftar",
        "bandingkan", "perbandingan", "antara", "dengan",
    }
    tokens = p.strip().split()
    cleaned = [t for t in tokens if t not in NON_LOCATION_WORDS]
    return " ".join(cleaned)
 
 
async def _resolve_vs_query(
    query: str,
    es_client: AsyncElasticsearch
) -> Optional[Any]:
    """
    Handle query dengan separator vs/versus/dan.
 
    Returns:
      - List[Dict]  → kedua sisi berhasil resolve sebagai lokasi berbeda
      - Dict {"status_comparison": True, ...} → perbandingan negeri vs swasta
      - Dict {"ambiguous": True, ...} → salah satu sisi ambigu
      - None  → tidak ada separator yang dikenal
    """
    q = query.lower().strip()
    
    # =========================
    # HANDLE "antara X dan Y"
    # =========================
    _BETWEEN_PATTERN = re.compile(
        r'\bantara\s+(.+?)\s+dan\s+(.+?)(?:\s+untuk|\s+mana|\s*$)',
        re.I
    )

    m = _BETWEEN_PATTERN.search(query)
    if m:
        left_raw = m.group(1).strip()
        right_raw = m.group(2).strip()
    else:
        # fallback ke logic separator lama
        left_raw, right_raw = None, None
    
    # Cari separator yang ada
    sep = None
    for s in VS_SEPARATORS:
        if s in q:
            sep = s
            break
    if not sep:
        return None
 
    if not left_raw or not right_raw:
        parts = q.split(sep, 1)
        if len(parts) != 2:
            return None
        left_raw, right_raw = parts[0].strip(), parts[1].strip()
 
    # ── FIX: Deteksi status sekolah SEBELUM membuang kata ─────
    status_left  = detect_status_sekolah(left_raw)
    status_right = detect_status_sekolah(right_raw)
 
    left_clean  = _clean_location_part(left_raw)
    right_clean = _clean_location_part(right_raw)
 
    logger.debug("[VS_RESOLVE] sep=%r left=%r right=%r status_left=%s status_right=%s",
                 sep, left_clean, right_clean, status_left, status_right)
 
    # ── Case A: Perbandingan STATUS dalam satu wilayah ────────
    # Contoh: "sekolah negeri vs swasta di cisauk"
    #         "negeri vs swasta cisauk"
    #         "cisauk negeri vs swasta"
    if status_left and status_right and status_left != status_right:
        # Kedua sisi adalah status, bukan lokasi → cari lokasi dari full query
        pseudo = re.sub(
            r"\b(negeri|swasta|vs|versus|dan|bandingkan|perbandingan|jumlah|sekolah|di|daftar|berapa|total)\b",
            "", q
        ).strip()
        pseudo = re.sub(r"\s+", " ", pseudo).strip()
 
        if pseudo:
            pseudo_query = f"jumlah sekolah di {pseudo}"
        else:
            # Tidak ada nama wilayah → tidak bisa resolve
            return None
 
        logger.debug("[VS_RESOLVE] status_comparison pseudo_query=%s", pseudo_query)
        sub = await resolve_entities(pseudo_query, es_client)
 
        if sub["status"] == ResultStatus.SUCCESS:
            entities = sub["data"].get("entities", {})
            if isinstance(entities, dict) and entities:
                return {
                    "status_comparison": True,
                    "entities": entities,
                    "statuses": [status_left, status_right],
                }
 
        elif sub["status"] == ResultStatus.AMBIGUOUS:
            candidates = sub.get("data", {}).get("candidates", [])
            # `pseudo` adalah nama lokasi yang diekstrak dari query di Case A
            message = _format_disambiguation_message(pseudo, candidates)

            return {
                "ambiguous": True,
                "ambiguous_part": pseudo,
                "candidates": candidates,
                "message": message
            }

        return None

    # ── Case B: Perbandingan STATUS implisit (satu sisi punya status) ──
    # Contoh: "cisauk negeri vs swasta" → left=cisauk negeri, right=swasta
    # Artinya: user mau negeri vs swasta di cisauk
    if (status_left and not status_right and not left_clean.replace(status_left.lower(), "").strip()) \
    or (status_right and not status_left and not right_clean.replace(status_right.lower(), "").strip()):
        # Salah satu sisi HANYA berisi status → cari lokasi dari sisi lainnya
        loc_part = right_clean if status_left else left_clean
        st_a = status_left or "Negeri"
        st_b = status_right or "Swasta"
 
        if loc_part:
            pseudo_query = f"jumlah sekolah di {loc_part}"
            logger.debug("[VS_RESOLVE] implicit_status pseudo_query=%s", pseudo_query)
            sub = await resolve_entities(pseudo_query, es_client)
 
            if sub["status"] == ResultStatus.SUCCESS:
                entities = sub["data"].get("entities", {})
                if isinstance(entities, dict) and entities:
                    return {
                        "status_comparison": True,
                        "entities": entities,
                        "statuses": [st_a, st_b],
                    }
 
            elif sub["status"] == ResultStatus.AMBIGUOUS:
                candidates = sub.get("data", {}).get("candidates", [])
                return {
                    "ambiguous": True,
                    "ambiguous_part": loc_part,
                    "candidates": candidates
                }
 
        return None
 
    # ── Case B2: Perbandingan dua JENJANG di satu lokasi ──────
    # Contoh: "bandingkan sma dan smk di kota bandung"
    #         "sma vs smk di bandung"
    # Kedua sisi adalah kode jenjang, bukan nama lokasi
    _JENJANG_TYPES = {
        "sd", "mi", "smp", "mts", "sma", "smk", "ma", "tk",
        "paud", "kb", "ra", "sps", "tpa",
        "sdn", "smpn", "sman", "smkn", "man", "mtsn", "min",
    }
    _left_tokens  = set(left_clean.split())
    _right_tokens = set(right_clean.split())
 
    # left adalah murni jenjang (semua token-nya ada di JENJANG_TYPES)
    _left_is_jenjang  = bool(_left_tokens)  and _left_tokens.issubset(_JENJANG_TYPES)
    # right bisa jenjang + lokasi, atau murni jenjang
    _right_jenjang_tokens = _right_tokens & _JENJANG_TYPES
    _right_loc_tokens     = _right_tokens - _JENJANG_TYPES - {"di", "kota", "kab", "kab.", "kec."}
    _right_has_jenjang    = bool(_right_jenjang_tokens)
 
    if _left_is_jenjang and _right_has_jenjang:
        # Ekstrak kedua jenjang
        jenjang_a = left_clean.strip().upper()
        jenjang_b = " ".join(_right_jenjang_tokens).strip().upper()
 
        # Ekstrak lokasi: buang semua token jenjang + kata noise dari full query
        _noise = _JENJANG_TYPES | {
            "bandingkan", "perbandingan", "vs", "versus", "dan", "dengan",
            "di", "sekolah", "jumlah", "berapa", "total",
        }
        _loc_tokens = [
            t for t in q.split()
            if t not in _noise and len(t) > 2
        ]
        loc_text = " ".join(_loc_tokens).strip()
 
        if loc_text:
            logger.debug(
                "[VS_RESOLVE] jenjang_comparison: %s vs %s | lokasi=%r",
                jenjang_a, jenjang_b, loc_text
            )
            pseudo_query = f"jumlah sekolah di {loc_text}"
            sub = await resolve_entities(pseudo_query, es_client)
 
            if sub["status"] == ResultStatus.SUCCESS:
                entities = sub["data"].get("entities", {})
                if isinstance(entities, dict) and entities:
                    return {
                        "jenjang_comparison": True,
                        "entities": entities,
                        "jenjang_list": [jenjang_a, jenjang_b],
                    }
 
            elif sub["status"] == ResultStatus.AMBIGUOUS:
                candidates = sub.get("data", {}).get("candidates", [])
                return {
                    "ambiguous": True,
                    "ambiguous_part": loc_text,
                    "candidates": candidates,
                }
 
        # Tidak ada lokasi di query → tidak bisa resolve jenjang comparison
        return None
 
    # ── Case C: Perbandingan dua LOKASI berbeda ───────────────
    locations = []
    for loc_text in [left_clean, right_clean]:
        if not loc_text:
            continue
 
        # ── Fuzzy correction untuk typo single-word location ────────────
        # Contoh: "parunpanjang" → "parungpanjang"
        # Hanya berlaku jika loc_text adalah satu kata (typo sederhana)
        corrected_text = loc_text
        tokens = loc_text.strip().split()
        if len(tokens) == 1:
            fuzzy_result = fuzzy_match_location(tokens[0])
            if fuzzy_result:
                matched_name, matched_level = fuzzy_result
                corrected_text = matched_name
                logger.info(
                    "[VS_RESOLVE] fuzzy corrected: '%s' → '%s' (level=%s)",
                    loc_text, corrected_text, matched_level
                )
        elif len(tokens) >= 2:
            # Multi-kata: coba koreksi setiap token secara individual
            # lalu gabung kembali — berguna untuk "parunpanjang bogor"
            corrected_text = fuzzy_correct_words(loc_text)
 
            if corrected_text != loc_text:
                logger.info(
                    "[VS_RESOLVE] fuzzy multi-word corrected: '%s' → '%s'",
                    loc_text, corrected_text
                )
 
        pseudo_query = f"jumlah sekolah di {corrected_text}"
        logger.debug("[VS_RESOLVE] location pseudo_query=%s", pseudo_query)
 
        sub = await resolve_entities(pseudo_query, es_client)
        logger.debug(
            "[VS_RESOLVE] sub status=%s entities=%s metadata=%s",
            sub["status"],
            sub.get("data", {}).get("entities"),
            sub.get("metadata")
        )
 
        if sub["status"] == ResultStatus.SUCCESS:
            entities = sub["data"].get("entities", {})
            if isinstance(entities, list) and entities:
                # area_group: gabungkan semua kecamatan dalam satu pseudo-entity
                # ambil kabupaten+provinsi dari elemen pertama saja
                first = entities[0]
                locations.append({
                    "kabupaten": first.get("kabupaten"),
                    "provinsi":  first.get("provinsi"),
                    "_area_group": sub.get("metadata", {}).get("area_group", ""),
                    "_kecamatan_list": [e.get("kecamatan") for e in entities if e.get("kecamatan")],
                })
            elif isinstance(entities, dict) and entities:
                locations.append(entities)
 
        elif sub["status"] == ResultStatus.AMBIGUOUS:
            candidates = sub.get("data", {}).get("candidates", [])
            # Gunakan corrected_text di pesan ambiguous agar lebih jelas
            return {
                "ambiguous": True,
                "ambiguous_part": corrected_text,
                "candidates": candidates
            }
 
        elif sub["status"] == ResultStatus.NOT_FOUND and corrected_text != loc_text:
            # Fuzzy correction tidak membantu — coba original text juga
            logger.debug("[VS_RESOLVE] fuzzy correction failed, trying original: %s", loc_text)
            pseudo_orig = f"jumlah sekolah di {loc_text}"
            sub_orig = await resolve_entities(pseudo_orig, es_client)
            if sub_orig["status"] == ResultStatus.SUCCESS:
                entities = sub_orig["data"].get("entities", {})
                if isinstance(entities, dict) and entities:
                    locations.append(entities)
 
    return locations if len(locations) >= 2 else None
