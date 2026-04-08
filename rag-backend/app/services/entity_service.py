# entity_service.py
import re
import logging
import os
import math
from typing import Dict, Any, Optional, List
from elasticsearch import AsyncElasticsearch
from rapidfuzz import fuzz
 
from app.services.location_service import LOCATION_INDEX, LOCATION_HIERARCHY, enrich_hierarchy_from_index
from app.services.core.result_model import build_result, ResultStatus
from app.services.core.aggregation_core import get_entity_doc_count, get_entity_doc_counts_batch
 
logger = logging.getLogger(__name__)
 
# =========================================================
# CONSTANTS
# =========================================================
 
MIN_FUZZY_SCORE = float(os.getenv("MIN_FUZZY_SCORE", 3))
FUZZY_THRESHOLD = 87
MAX_FUZZY_CANDIDATES = 5
 
ALL_LOCATIONS_CACHE = []
 
LOCATION_HINT_WORDS = {
    "provinsi", "kabupaten", "kota",
    "kecamatan", "desa", "kelurahan"
}
 
LOCATION_FIELDS = [
    "provinsi",
    "kabupaten",
    "kota",
    "kecamatan",
    "desa_kelurahan"
]
 
HIERARCHY_ORDER = [
    "provinsi",
    "kabupaten",
    "kota",
    "kecamatan",
    "desa_kelurahan"
]
 
LEVEL_LABEL = {
    "provinsi": "Provinsi",
    "kabupaten": "Kabupaten",
    "kota": "Kota",
    "kecamatan": "Kecamatan",
    "desa_kelurahan": "Desa/Kelurahan"
}
 
LEVEL_PRIORITY = {
    "provinsi": 4,
    "kabupaten": 3,
    "kota": 3,
    "kecamatan": 2,
    "desa_kelurahan": 1
}
 
GENERIC_LOCATION_TERMS = {
    "jawa",
    "sumatera",
    "kalimantan",
    "sulawesi",
    "papua",
    "indonesia"
}

COMMON_NOUNS_BLOCKLIST = {
    "bangunan", "jalan", "kampung", "pasar", "sawah", "ladang",
    "pantai", "gunung", "bukit", "sungai", "danau", "hutan",
    "perumahan", "industri", "pertanian", "perdagangan",
    "barat", "timur", "utara", "selatan", "tengah",  # arah mata angin standalone
}
JAKARTA_ALIAS = [
    # Singkatan kota administrasi (harus sebelum pola "jakarta" panjang)
    (r"\bjakpus\b",            "kota adm. jakarta pusat",   "kabupaten"),
    (r"\bjakbar\b",            "kota adm. jakarta barat",   "kabupaten"),
    (r"\bjaksel\b",            "kota adm. jakarta selatan", "kabupaten"),
    (r"\bjaktim\b",            "kota adm. jakarta timur",   "kabupaten"),
    (r"\bjakut\b",             "kota adm. jakarta utara",   "kabupaten"),
    # Nama panjang
    (r"\bjakarta\s+pusat\b",   "kota adm. jakarta pusat",   "kabupaten"),
    (r"\bjakarta\s+barat\b",   "kota adm. jakarta barat",   "kabupaten"),
    (r"\bjakarta\s+selatan\b", "kota adm. jakarta selatan", "kabupaten"),
    (r"\bjakarta\s+timur\b",   "kota adm. jakarta timur",   "kabupaten"),
    (r"\bjakarta\s+utara\b",   "kota adm. jakarta utara",   "kabupaten"),
    # DKI → provinsi (urutan paling bawah agar tidak override kota adm.)
    (r"\bdki\s+jakarta\b",     "prov. dki jakarta",         "provinsi"),
    (r"\bdki\b",               "prov. dki jakarta",         "provinsi"),
]
# Area group: nama kota populer yang terdiri dari beberapa kecamatan
AREA_GROUP: Dict[str, Dict[str, Any]] = {
    "purwokerto": {
        "label":          "Purwokerto (Kab. Banyumas)",
        "kabupaten":      "kab. banyumas",
        "provinsi":       "prov. jawa tengah",
        "kecamatan_list": [
            "kec. purwokerto timur",
            "kec. purwokerto barat",
            "kec. purwokerto utara",
            "kec. purwokerto selatan",
        ],
    },
    # Tambah di sini jika ada kota lain yang serupa
}
 
DESA_BLACKLIST = {
    "--", "-", "n/a", "na", "null", "",
    "desa", "kelurahan", "kota", "kabupaten", "kecamatan",
    "provinsi", "prov", "kab", "kec",
}
# =========================================================
# NORMALIZATION
# =========================================================
def normalize_wilayah_value(value: str) -> str:
    if not value:
        return value
 
    value = value.lower().strip()
 
    replacements = {
        "kabupaten ": "kab. ",
        "provinsi ": "prov. ",
        "kecamatan ": "kec. ",
        "kelurahan ": "",
        "desa ": ""
    }
 
    for old, new in replacements.items():
        value = value.replace(old, new)
 
    return value.strip()
 
 
def normalize_query(query: str) -> str:
    q = re.sub(r"[^\w\s]", " ", query.lower())
    return re.sub(r"\s+", " ", q).strip()
 
def normalize_location_prefix(query: str) -> str:
    return normalize_query(query)
 
def _strip_loc_prefix(value: str) -> str:
    """Strip prefix lokasi standar + 'adm.' dari nama wilayah."""
    value = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", value)
    value = re.sub(r"^adm\.\s*", "", value)
    return value.strip()
 
def detect_level_from_value(value: str) -> Optional[str]:
    if not value:
        return None
 
    value = value.lower().strip()
 
    if re.match(r"^(kab\.?|kabupaten)\b", value):
        return "kabupaten"
 
    if re.match(r"^(kota|adm\.)\b", value):
        return "kota"
 
    return None
 
def attach_status(entity, status_sekolah):
 
    if status_sekolah and "status_sekolah" not in entity:
        entity["status_sekolah"] = status_sekolah
 
    return entity
 
 
# =========================================================
# NEW: EXTRACT CONTEXT KABUPATEN/PROVINSI FROM QUERY
# Digunakan untuk disambiguasi desa level.
# Contoh: "desa cibunar bogor" → konteks kabupaten = "bogor"
# =========================================================
 
def _extract_kabupaten_context(query: str) -> Optional[str]:
    """
    Cari kabupaten/kota yang disebutkan eksplisit di query,
    untuk digunakan sebagai konteks filter saat disambiguasi desa.
    Return: nilai kabupaten normalized (e.g. "kab. bogor") atau None.
    """
    q = normalize_query(query)
 
    kab_lookup = LOCATION_INDEX.get("kabupaten", {})
    if not kab_lookup:
        return None
 
    # Sort panjang dulu
    sorted_keys = sorted(kab_lookup.items(), key=lambda x: len(x[0]), reverse=True)
 
    for key, raw in sorted_keys:
        key_clean = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key)
        key_norm  = normalize_query(key_clean)
 
        if not key_norm:
            continue
 
        if re.search(r"\b" + re.escape(key_norm) + r"\b", q):
            return normalize_wilayah_value(raw)   # e.g. "kab. bogor"
 
    return None
 
 
def _extract_provinsi_context(query: str) -> Optional[str]:
    """
    Cari provinsi yang disebutkan di query.
    Return: nilai provinsi normalized (e.g. "prov. jawa barat") atau None.
    """
    q = normalize_query(query)
 
    prov_lookup = LOCATION_INDEX.get("provinsi", {})
    if not prov_lookup:
        return None
 
    sorted_keys = sorted(prov_lookup.items(), key=lambda x: len(x[0]), reverse=True)
 
    for key, raw in sorted_keys:
        key_clean = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key)
        key_norm  = normalize_query(key_clean)
 
        if not key_norm:
            continue
 
        if re.search(r"\b" + re.escape(key_norm) + r"\b", q):
            return normalize_wilayah_value(raw)
 
    return None
 
 
def _filter_desa_by_context(
    candidates: List[Dict],
    kab_context: Optional[str],
    prov_context: Optional[str]
) -> List[Dict]:
    """
    Filter kandidat desa berdasarkan konteks kabupaten/provinsi yang
    disebutkan user di query.
 
    Contoh:
      candidates = [cibunar @ Kab. Garut, cibunar @ Kab. Bogor]
      kab_context = "kab. bogor"
      → return [cibunar @ Kab. Bogor]
 
    Fallback: jika filter menghasilkan kosong, kembalikan candidates asli
    (jangan sampai stuck NOT_FOUND karena typo).
    """
    if not candidates:
        return candidates
 
    filtered = []
 
    def _strip_prefix_compare(val: str) -> str:
        """Strip kab./kota/prov./kec. prefix dan lowercase untuk perbandingan."""
        return re.sub(
            r"^(kab\.?|kota|prov\.?|provinsi|kabupaten|kecamatan|kec\.?)\s*",
            "", val.lower()
        ).strip()
 
    for c in candidates:
        hier = c.get("hierarchy", {})
 
        # Match kabupaten context
        if kab_context:
            # hier["kabupaten"] bisa "Kab. Bogor" atau "Kota Bogor" (dari enrich)
            hier_kab_raw  = hier.get("kabupaten", "")
            hier_kab_name = _strip_prefix_compare(hier_kab_raw)
            ctx_kab_name  = _strip_prefix_compare(kab_context)
 
            if ctx_kab_name and hier_kab_name and ctx_kab_name in hier_kab_name:
                filtered.append(c)
                continue
 
        # Match provinsi context (fallback jika tidak ada kab context)
        if prov_context and not kab_context:
            hier_prov_raw  = hier.get("provinsi", "")
            hier_prov_name = _strip_prefix_compare(hier_prov_raw)
            ctx_prov_name  = _strip_prefix_compare(prov_context)
 
            if ctx_prov_name and hier_prov_name and ctx_prov_name in hier_prov_name:
                filtered.append(c)
                continue
 
    # Jika hasil filter kosong → kembalikan asli (safety fallback)
    if not filtered:
        logger.warning(
            "[DESA CONTEXT] No match for kab=%s prov=%s, "
            "fallback to original %d candidates",
            kab_context, prov_context, len(candidates)
        )
        return candidates
 
    logger.info(
        "[DESA CONTEXT] Filtered %d → %d candidates (kab=%s)",
        len(candidates), len(filtered), kab_context
    )
    return filtered
 
# =========================================================
# VS QUERY SPLITTER
# =========================================================
def split_vs_query(query: str) -> Optional[List[str]]:
    """
    Split query comparison jadi 2 bagian berdasarkan vs/versus.
    Contoh:
      "jumlah sekolah di cibunar parungpanjang vs cibunar cibatu"
      → ["cibunar parungpanjang", "cibunar cibatu"]
    """
 
    q = query.lower()
 
    for sep in [" vs ", " versus "]:
        if sep in q:
            parts = q.split(sep, 1)
 
            cleaned = []
            for p in parts:
                p = re.sub(
                    r'\b(jumlah|berapa|total|sekolah|di|daftar|negeri|swasta)\b',
                    '',
                    p
                ).strip()
 
                cleaned.append(p)
 
            if all(cleaned):
                return cleaned
 
    return None
 
# =========================================================
# DETERMINISTIC EXTRACTION
# =========================================================
 
def extract_entities(query: str) -> Any:
 
    cleaned = normalize_query(query)
    tokens = cleaned.split()
    search_order = determine_search_order(cleaned)
 
    entities: Dict[str, Any] = {}
    cross_level_matches = []
 
    for level in search_order:
 
        resolved_list = match_location_level(cleaned, level)
 
        if len(resolved_list) == 1:
            entities[level] = resolved_list[0]
            cross_level_matches.append((level, resolved_list[0]))
 
        elif len(resolved_list) > 1:
            return build_result(
                status=ResultStatus.AMBIGUOUS,
                reason="MULTIPLE_MATCHES",
                metadata={
                    "options": [
                        f"{LEVEL_LABEL.get(level)} {v}"
                        for v in resolved_list
                    ]
                }
            )
 
    if len(cross_level_matches) > 1:
        return build_result(
            status=ResultStatus.AMBIGUOUS,
            reason="CROSS_LEVEL_AMBIGUOUS",
            metadata={
                "options": [
                    f"{LEVEL_LABEL.get(lvl)} {val}"
                    for lvl, val in cross_level_matches
                ]
            }
        )
 
    if "negeri" in tokens:
        entities["status_sekolah"] = "NEGERI"
    elif "swasta" in tokens:
        entities["status_sekolah"] = "SWASTA"
 
    year = re.search(r"\b(20\d{2})\b", cleaned)
    if year:
        entities["tahun"] = int(year.group(1))
 
    if "provinsi" not in entities:
        prov_lookup = LOCATION_INDEX.get("provinsi", {})
        for key, raw in prov_lookup.items():
            stripped = normalize_wilayah_value(key)
            stripped = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", stripped)
 
            if stripped and re.search(r"\b" + re.escape(stripped) + r"\b", cleaned):
                entities["provinsi"] = normalize_wilayah_value(raw)
                break
 
    return entities
 
 
def determine_search_order(query: str) -> List[str]:
 
    if re.search(r"\b(kecamatan|kec\.)\b", query):
        return ["kecamatan"]
 
    if re.search(r"\b(desa|kelurahan)\b", query):
        return ["desa_kelurahan"]
 
    if re.search(r"\bkota\b", query):
        return ["kota"]
 
    if re.search(r"\b(kabupaten|kab\.)\b", query):
        return ["kabupaten"]
 
    if re.search(r"\bprovinsi\b", query):
        return ["provinsi"]
 
    return ["provinsi", "kabupaten", "kota"]
 
 
def match_location_level(query: str, level: str) -> List[str]:
    lookup = LOCATION_INDEX.get(level, {})
    if not lookup:
        return []
 
    cleaned_query = normalize_query(query)
 
    matches = []
 
    explicit_prefix = {
        "kabupaten": r"\b(kabupaten|kab\.)\s+(.+)",
        "kota": r"\bkota\s+(.+)",
        "provinsi": r"\bprovinsi\s+(.+)"
    }
 
    if level in explicit_prefix:
        m = re.search(explicit_prefix[level], cleaned_query)
        if m:
 
            if level == "kabupaten":
                candidate = m.group(2).strip()
            else:
                candidate = m.group(1).strip()
 
            candidate_norm = normalize_query(candidate)
 
            for key, value in lookup.items():
 
                key_norm = normalize_query(
                    re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key)
                )
 
                if candidate_norm == key_norm:
                    matches.append(value)
 
            return list(set(matches))
 
    for key, value in lookup.items():
 
        key_norm = normalize_query(
            re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key)
        )
 
        if re.search(r"\b" + re.escape(key_norm) + r"\b", cleaned_query):
 
            detected = detect_level_from_value(value)
 
            if detected and detected != level:
                continue
 
            matches.append(value)
 
    return list(set(matches))
 
# =========================================================
# MULTI LOCATION
# =========================================================
 
def extract_multiple_locations(query: str, requested_level: Optional[str] = None) -> List[Dict[str, str]]:
 
    cleaned = normalize_query(query)
 
    results = []
 
    for level in LOCATION_FIELDS:
 
        if requested_level and level != requested_level:
            continue
 
        lookup = LOCATION_INDEX.get(level, {})
 
        for key, raw in lookup.items():
 
            key_clean = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key)
            key_norm = normalize_query(key_clean)
 
            if re.search(r"\b" + re.escape(key_norm) + r"\b", cleaned):
                results.append({
                    "level": level,
                    "value": normalize_wilayah_value(raw)
                })
 
    unique = {}
    for item in results:
        unique[(item["level"], item["value"])] = item
 
    return list(unique.values())
 
 
# =========================================================
# VALIDATION
# =========================================================
 
def validate_location_presence(entities: Dict[str, Any]) -> bool:
    return any(field in entities for field in LOCATION_FIELDS)
 
 
def might_contain_location(query: str) -> bool:
    tokens = query.lower().split()
    return any(word in tokens for word in LOCATION_HINT_WORDS)
 
 
def validate_hierarchy_integrity(entities: Dict[str, Any]) -> Dict[str, Any]:
 
    if not entities:
        return build_result(status=ResultStatus.SUCCESS)
 
    if "kecamatan" in entities and "kabupaten" not in entities:
        return build_result(
            status=ResultStatus.VALIDATION_ERROR,
            metadata={"warning": None},
            reason="HIERARCHY_INCOMPLETE"
        )
 
    if "desa_kelurahan" in entities and "kecamatan" not in entities:
        return build_result(
            status=ResultStatus.VALIDATION_ERROR,
            reason="HIERARCHY_INCOMPLETE",
            metadata={
                "missing_parent": "kecamatan",
                "level": "desa_kelurahan"
            }
        )
 
    return build_result(
        status=ResultStatus.SUCCESS,
        metadata={"hierarchy_warning": None}
    )
 
# =========================================================
# HELPER FOR COMPARISON HANDLER
# =========================================================
def detect_location_level_from_query(query: str) -> Optional[str]:
 
    q = normalize_query(query)
 
    if re.search(r"\bprovinsi\b", q):
        return "provinsi"
 
    if re.search(r"\b(kabupaten|kab\.)\b", q):
        return "kabupaten"
 
    if re.search(r"\bkota\b", q):
        return "kota"
 
    if re.search(r"\b(kecamatan|kec\.)\b", q):
        return "kecamatan"
 
    if re.search(r"\b(desa|kelurahan)\b", q):
        return "desa_kelurahan"
 
    return None
 
def build_candidate(item):
    # Jika item sudah punya hierarchy lengkap (dari find_all_location_matches)
    # gunakan langsung — jangan enrich ulang karena enrich_hierarchy_from_index
    # hanya ambil entries[0] yang mungkin salah untuk desa ambiguous
    if item.get("hierarchy") and len(item["hierarchy"]) > 1:
        entity = item["hierarchy"]
    else:
        entity = enrich_hierarchy_from_index({
            item["level"]: normalize_wilayah_value(item["value"])
        })
    return {
        "level": item["level"],
        "label": build_readable_label(entity),
        "hierarchy": entity
    }
 
# =========================================================
# VALIDATION FUNCTIONS
# =========================================================
 
def validate_hierarchy_consistency(query: str, entities: dict) -> bool:
 
    requested_level = detect_location_level_from_query(query)
 
    if not requested_level:
        return True
 
    if requested_level == "kota":
        return "kabupaten" in entities and entities["kabupaten"].startswith("kota")
 
    return requested_level in entities
 
 
# =========================================================
# SMART DISAMBIGUATION HELPERS
# =========================================================
 
def find_all_location_matches(name: str) -> List[Dict[str, str]]:
 
    cleaned = normalize_query(name)
    matches = []
 
    for level in LOCATION_FIELDS:
 
        if level == "desa_kelurahan":
            # ── Untuk desa: gunakan LOCATION_HIERARCHY agar
            # semua desa dengan nama sama di beda wilayah ditemukan ──
            hier = LOCATION_HIERARCHY.get("desa_kelurahan", {})
 
            for desa_key, entries in hier.items():
            # Skip nilai noise
                if desa_key.strip() in DESA_BLACKLIST or len(desa_key.strip()) < 2:
                    continue
                # desa_key = "cibunar", "parungpanjang", dst (lowercase)
                key_norm = normalize_query(desa_key)
 
                if not re.search(r"\b" + re.escape(key_norm) + r"\b", cleaned):
                    continue
 
                # Setiap entry adalah satu lokasi unik (kec+kab berbeda)
                for entry in entries:
                    matches.append({
                        "level": "desa_kelurahan",
                        "value": desa_key,          # nama desa lowercase
                        "hierarchy": {
                            "desa_kelurahan": desa_key,
                            "kecamatan":      entry.get("kecamatan", ""),
                            "kabupaten":      entry.get("kabupaten", ""),
                            "provinsi":       entry.get("provinsi", ""),
                        }
                    })
 
        else:
            # ── Untuk level lain: tetap gunakan LOCATION_INDEX ──
            lookup = LOCATION_INDEX.get(level, {})
            if not lookup:
                continue
 
            sorted_keys = sorted(
                lookup.items(),
                key=lambda x: len(x[0]),
                reverse=True
            )
 
            multi_word_matches = []
            single_word_matches = []
 
            for key, raw in sorted_keys:
 
                key_clean = _strip_loc_prefix(key)          # ← UBAH
                key_norm  = normalize_query(key_clean)
 
                if re.search(r"\b" + re.escape(key_norm) + r"\b", cleaned):
 
                    if len(key_norm.split()) > 1:
                        multi_word_matches.append({
                            "level": level,
                            "value": normalize_wilayah_value(raw)
                        })
                    else:
                        single_word_matches.append({
                            "level": level,
                            "value": normalize_wilayah_value(raw)
                        })
 
            if multi_word_matches:
                matches.extend(multi_word_matches)
            else:
                matches.extend(single_word_matches)
 
    # Deduplikasi: untuk desa, key = (level, value, kecamatan)
    # untuk lain, key = (level, value)
    unique = {}
    for m in matches:
        if m["level"] == "desa_kelurahan":
            kec = m.get("hierarchy", {}).get("kecamatan", "")
            key = (m["level"], m["value"], kec)
        else:
            key = (m["level"], m["value"])
        unique[key] = m
 
    return list(unique.values())
 
 
def build_readable_label(entity: Dict[str, str]) -> str:
 
    parts = []
 
    if "desa_kelurahan" in entity:
        parts.append(entity["desa_kelurahan"])
 
    if "kecamatan" in entity:
        parts.append(entity["kecamatan"])
 
    if "kabupaten" in entity:
        parts.append(entity["kabupaten"])
 
    if "provinsi" in entity:
        parts.append(entity["provinsi"])
 
    return ", ".join(parts)
 
def extract_requested_level(query: str) -> Optional[str]:
 
    query = query.lower()
    # Jika ada "desa/kelurahan" DAN "kecamatan" sekaligus →
    # prioritaskan desa (gunakan kecamatan sebagai konteks filter)
    has_desa = bool(re.search(r"\b(desa|kelurahan)\b", query))
    has_kec  = bool(re.search(r"\b(kecamatan|kec\.)\b", query))
    if has_desa and has_kec:
        return "desa_kelurahan"
    
    if re.search(r"\bprovinsi\b", query):
        return "provinsi"
 
    if re.search(r"\b(kabupaten|kab\.)\b", query):
        return "kabupaten"
 
    if re.search(r"\bkota\b", query):
        return "kota"
 
    if re.search(r"\b(kecamatan|kec\.)\b", query):
        return "kecamatan"
 
    if re.search(r"\b(desa|kelurahan)\b", query):
        return "desa_kelurahan"
 
    tokens = query.split()
 
    for word in tokens:
 
        if fuzz.ratio(word, "kabupaten") > 85:
            return "kabupaten"
 
        if fuzz.ratio(word, "provinsi") > 85:
            return "provinsi"
 
        if fuzz.ratio(word, "kecamatan") > 85:
            return "kecamatan"
 
        if fuzz.ratio(word, "kelurahan") > 85:
            return "desa_kelurahan"
 
        if fuzz.ratio(word, "desa") > 85:
            return "desa_kelurahan"
 
    return None
 
# =========================================================
# BUILD REFERENCE LOCATIONS FOR FUZZY FALLBACK
# =========================================================
def build_reference_locations():
    global ALL_LOCATIONS_CACHE
 
    if ALL_LOCATIONS_CACHE:
        return ALL_LOCATIONS_CACHE
 
    refs = []
 
    for level, lookup in LOCATION_INDEX.items():
        for key, raw in lookup.items():
 
            stripped = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key)
            normalized_name = normalize_query(stripped)
 
            refs.append({
                "value": normalize_wilayah_value(raw),
                "level": level,
                "normalized_name": normalized_name
            })
 
    ALL_LOCATIONS_CACHE = refs
    return refs

# Di fungsi compute_entity_ranking_score atau sebelum memasukkan match:
def _is_common_noun(value: str) -> bool:
    clean = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", value).strip().lower()
    return clean in COMMON_NOUNS_BLOCKLIST

# =========================================================
# ENTITY HIERARCHY NORMALIZATION
# =========================================================
def compute_entity_ranking_score(match: dict, query: str) -> float:
 
    q = normalize_query(query)
 
    name = normalize_query(
        re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", match["value"])
    )
 
    lexical_score = fuzz.ratio(name, q) / 100.0
 
    level_score = LEVEL_PRIORITY.get(match["level"], 0) / max(LEVEL_PRIORITY.values())
 
    name_words = name.split()
    coverage = sum(1 for w in name_words if w in q)
    coverage_score = coverage / len(name_words) if name_words else 0
 
    data_strength = match.get("data_strength_score", 0)
 
    prefix_bonus = 0.0
 
    value_lower = match["value"].lower()
 
    if "kota" in q and value_lower.startswith("kota"):
        prefix_bonus = 1.0
    elif "kabupaten" in q and value_lower.startswith(("kab.", "kabupaten")):
        prefix_bonus = 1.0
    elif "kecamatan" in q and match["level"] == "kecamatan":
        prefix_bonus = 1.0
    elif "desa" in q and match["level"] == "desa_kelurahan":
        prefix_bonus = 1.0
 
    hierarchy_score = 0.0
 
    for level in ["provinsi", "kabupaten", "kecamatan"]:
        if level in q and level in match.get("hierarchy", {}):
            hierarchy_score += 0.3
 
    hierarchy_score = min(hierarchy_score, 1.0)
 
    final_score = (
        0.25 * lexical_score +
        0.15 * level_score +
        0.25 * data_strength +
        0.15 * coverage_score +
        0.10 * prefix_bonus +
        0.10 * hierarchy_score
    )
    
    # Penalti desa kecil (noise reduction)
    if match["level"] == "desa_kelurahan" and match.get("doc_count", 0) < 10:
        final_score *= 0.3
        
    return round(final_score, 4)
 
# =========================================================
# MAIN RESOLUTION (CLEAN VERSION)
# =========================================================
 
async def resolve_entities(
    query: str,
    es_client: AsyncElasticsearch
) -> Dict[str, Any]:
 
    query = normalize_location_prefix(query)
    cleaned = normalize_location_prefix(query)
 
    logger.debug("QUERY CLEANED: %s", cleaned)
    # =====================================================
    # VS SPLIT HANDLER
    # =====================================================
    vs_parts = split_vs_query(cleaned)
 
    if vs_parts:
        # GUARD: jika query mengandung nama sekolah spesifik (SMAN, SMPN, SDN, dll)
        # jangan proses sebagai vs_split lokasi — ini comparison sekolah, bukan lokasi
        _SCHOOL_NAME_PATTERN = re.compile(
            r'\b(sd|mi|smp|mts|sma|smk|ma|sman|smpn|sdn|smkn|man|mtsn|min)'
            r'\s*(?:negeri|swasta)?\s*\d',
            re.IGNORECASE
        )
 
        if _SCHOOL_NAME_PATTERN.search(cleaned):
            logger.debug(
                "[VS_SPLIT] Skip — query contains school name pattern: %s", cleaned
            )
            vs_parts = None  # batalkan vs_split
 
    # hanya lanjut jika vs_parts masih valid
    if vs_parts:
        entities_list = []
 
        for part in vs_parts:
            sub_matches = find_all_location_matches(part)
 
            if not sub_matches:
                continue
 
            best = sub_matches[0]
 
            entity = {
                best["level"]: normalize_wilayah_value(best["value"])
            }
 
            entity = enrich_hierarchy_from_index(entity)
 
            entities_list.append(entity)
 
        if len(entities_list) >= 2:
            return build_result(
                status=ResultStatus.SUCCESS,
                data={
                    "entities": entities_list,
                    "is_multi": True
                },
                metadata={"vs_split": True}
            )
    # ============================================
    # DETECT SCHOOL STATUS
    # ============================================
 
    status_sekolah = None
 
    if re.search(r"\bnegeri\b", cleaned):
        status_sekolah = "NEGERI"
    elif re.search(r"\bswasta\b", cleaned):
        status_sekolah = "SWASTA"
 
    logger.debug("DETECTED STATUS SEKOLAH: %s", status_sekolah)
 
    # ============================================
    # GENERIC LOCATION PROTECTION (EARLY GUARD)
    # ============================================
 
    #     # Strip lebih agresif: hapus semua kata non-lokasi umum
    stripped_query = re.sub(
        r"\b(berapa|jumlah|total|banyak|terbanyak|sekolah|di|ada|yang|"
        r"paling|dengan|memiliki|mana|wilayah|daerah)\b",
        "",
        cleaned
    )
    stripped_query = normalize_query(stripped_query)
    tokens = [t for t in stripped_query.split() if len(t) >= 3]
 
    # Jika semua token yang tersisa adalah generic terms → guard
    if tokens and all(t in GENERIC_LOCATION_TERMS for t in tokens):
        return build_result(
            status=ResultStatus.AMBIGUOUS,
            data={
                "message": "Lokasi terlalu umum",
                "suggestion": (
                    f"'{' '.join(tokens)}' mencakup banyak wilayah. "
                    "Silakan sebutkan provinsi atau kabupaten yang lebih spesifik."
                )
            },
            metadata={"reason": "generic_location"}
        )
 
    requested_level = extract_requested_level(cleaned)
    # =====================================================
    # JAKARTA ALIAS: resolve langsung sebelum possible_matches
    # dibangun — hindari 'kota jakarta barat' (noise, 4 docs)
    # mengalahkan 'kota adm. jakarta barat' (data utama, 2664 docs)
    # =====================================================
    for _pattern, _canonical, _lvl in JAKARTA_ALIAS:
        if re.search(_pattern, cleaned):
            _entity = enrich_hierarchy_from_index({_lvl: _canonical})
            _entity = attach_status(_entity, status_sekolah)
            logger.debug("[JAKARTA_ALIAS] '%s' → '%s'", _pattern, _canonical)
            return build_result(
                status=ResultStatus.SUCCESS,
                data={"entities": _entity},
                metadata={"location_alias": _canonical}
            )
    
    # =====================================================
    # JAKARTA STANDALONE AMBIGUOUS
    # "jakarta" tanpa suffix arah → tanya: provinsi atau kota adm. mana?
    # =====================================================
    _JAKARTA_STANDALONE = re.compile(r"\bjakarta\b", re.IGNORECASE)
    _JAKARTA_SUFFIX     = re.compile(
        r"\b(pusat|barat|selatan|timur|utara|dki)\b", re.IGNORECASE
    )
    if _JAKARTA_STANDALONE.search(cleaned) and not _JAKARTA_SUFFIX.search(cleaned):
        _jakarta_candidates = [
            {
                "level": "provinsi",
                "label": "DKI Jakarta (Provinsi — semua wilayah)",
                "hierarchy": {"provinsi": "prov. dki jakarta"},
            },
            {
                "level": "kabupaten",
                "label": "Jakarta Pusat",
                "hierarchy": {"kabupaten": "kota adm. jakarta pusat", "provinsi": "prov. dki jakarta"},
            },
            {
                "level": "kabupaten",
                "label": "Jakarta Selatan",
                "hierarchy": {"kabupaten": "kota adm. jakarta selatan", "provinsi": "prov. dki jakarta"},
            },
            {
                "level": "kabupaten",
                "label": "Jakarta Barat",
                "hierarchy": {"kabupaten": "kota adm. jakarta barat", "provinsi": "prov. dki jakarta"},
            },
            {
                "level": "kabupaten",
                "label": "Jakarta Timur",
                "hierarchy": {"kabupaten": "kota adm. jakarta timur", "provinsi": "prov. dki jakarta"},
            },
            {
                "level": "kabupaten",
                "label": "Jakarta Utara",
                "hierarchy": {"kabupaten": "kota adm. jakarta utara", "provinsi": "prov. dki jakarta"},
            },
        ]
        logger.debug("[JAKARTA_AMBIGUOUS] 'jakarta' standalone → AMBIGUOUS")
        return build_result(
            status=ResultStatus.AMBIGUOUS,
            data={"candidates": _jakarta_candidates},
            metadata={"reason": "jakarta_standalone_ambiguous"}
        )
    
    # =====================================================
    # AREA GROUP: kota populer = multi kecamatan sekaligus
    # Guard: kata arah (timur/barat/utara/selatan/tengah) →
    # skip, biarkan resolve ke kecamatan spesifik
    # =====================================================
    _direction_words = {"timur", "barat", "utara", "selatan", "tengah"}
    _query_tokens    = set(cleaned.split())
 
    for _area_key, _area_cfg in AREA_GROUP.items():
        _ag_pattern = r"\b" + re.escape(_area_key) + r"\b"
        if re.search(_ag_pattern, cleaned) and not (_query_tokens & _direction_words):
            _ent_list = []
            for _kec in _area_cfg["kecamatan_list"]:
                _ent = {
                    "kecamatan": _kec,
                    "kabupaten": _area_cfg["kabupaten"],
                    "provinsi":  _area_cfg["provinsi"],
                }
                _ent = attach_status(_ent, status_sekolah)
                _ent_list.append(_ent)
 
            logger.debug(
                "[AREA_GROUP] '%s' → %d kecamatan (%s)",
                _area_key, len(_ent_list), _area_cfg["label"]
            )
            return build_result(
                status=ResultStatus.SUCCESS,
                data={
                    "entities":   _ent_list,
                    "is_multi":   True,
                    "area_label": _area_cfg["label"],
                },
                metadata={"area_group": _area_key}
            )
            
    # =====================================================
    # MULTI ENTITY DETECTION (STRICT + LEVEL SAFE)
    # =====================================================
 
    if any(sep in cleaned for sep in [" dan ", ",", "&"]):
 
        multi_locations = extract_multiple_locations(cleaned, requested_level)
 
        if multi_locations:
 
            levels = set(m["level"] for m in multi_locations)
 
            if multi_locations:
 
                levels = set(m["level"] for m in multi_locations)
 
                if len(levels) == 1:
                    filtered = multi_locations
 
                elif not requested_level:
                    highest = max(levels, key=lambda l: LEVEL_PRIORITY.get(l, 0))
                    filtered = [m for m in multi_locations if m["level"] == highest]
 
                else:
                    return build_result(
                        status=ResultStatus.AMBIGUOUS,
                        metadata={"reason": "AMBIGUOUS_LEVEL_MIX"}
                    )
 
            if len(filtered) > 1:
 
                logger.debug("MULTI ENTITIES DETECTED (FILTERED): %s", filtered)
 
                entities_list = []
 
                for item in filtered:
                    entity = {
                        item["level"]: item["value"]
                    }
 
                    entity = enrich_hierarchy_from_index(entity)
                    entity = attach_status(entity, status_sekolah)
 
                    entities_list.append(entity)
 
                return build_result(
                    status=ResultStatus.SUCCESS,
                    data={
                        "entities": entities_list,
                        "is_multi": True
                    }
                )
 
    # =====================================================
    # SMART DISAMBIGUATION
    # =====================================================
 
    possible_matches = find_all_location_matches(cleaned)
    logger.debug("ALL POSSIBLE MATCHES: %s", possible_matches)

    # ── JENJANG NOISE FILTER ────────────────────────────────────────────────
    # Jika query mengandung kata yang juga nama jenjang pendidikan,
    # hapus kandidat wilayah yang value-nya persis sama dengan kata tersebut.
    # Contoh: "pesantren" di query → buang desa/kec bernilai "pesantren" murni.
    # Tapi "kec. pesantren" tetap lolos karena ada prefix "kec.".
    _JENJANG_NOISE_WORDS = {
        "pesantren", "pondok", "universitas", "institut",
        "akademi", "politeknik", "kursus", "pkbm",
    }
    _query_tokens_noise = set(normalize_query(cleaned).split())
    if _query_tokens_noise & _JENJANG_NOISE_WORDS:
        _before_jenjang = len(possible_matches)
        possible_matches = [
            m for m in possible_matches
            if m["value"].strip().lower() not in _JENJANG_NOISE_WORDS
        ]
        logger.debug(
            "[JENJANG NOISE FILTER] %d → %d",
            _before_jenjang, len(possible_matches)
        )
        # Jika ada provinsi yang match, buang juga kecamatan yang namanya
        # mengandung kata jenjang (misal kec. pesantren ikut terbuang)
        _prov_matches = [m for m in possible_matches if m["level"] == "provinsi"]
        if _prov_matches:
            _before_kec = len(possible_matches)
            possible_matches = [
                m for m in possible_matches
                if m["level"] == "provinsi"
                or not any(
                    noise in m["value"].lower()
                    for noise in _JENJANG_NOISE_WORDS
                )
            ]
            logger.debug(
                "[JENJANG KEC GUARD] %d → %d (provinsi found, removed jenjang-named kec)",
                _before_kec, len(possible_matches)
            )

    # =====================================================
    # FILTER COMMON NOUN (FIX NOISE: "barat", "jalan", dll)
    # =====================================================
    before_filter = len(possible_matches)

    possible_matches = [
        m for m in possible_matches
        if not _is_common_noun(m["value"])
    ]

    logger.debug(
        "[COMMON NOUN FILTER] %d → %d",
        before_filter, len(possible_matches)
    )
    
    # ============================================
    # DATA STRENGTH ENRICHMENT (BATCH MSEARCH)
    # ============================================
 
    # Build entity dict for each match (used for doc count query)
    match_entities = []
    for m in possible_matches:
        if m["level"] == "desa_kelurahan" and m.get("hierarchy"):
            entity = {
                "desa_kelurahan": m["value"],
                "kecamatan": m["hierarchy"].get("kecamatan", ""),
                "kabupaten": m["hierarchy"].get("kabupaten", ""),
                "provinsi":  m["hierarchy"].get("provinsi", ""),
            }
            entity = {k: v for k, v in entity.items() if v}
        else:
            entity = {m["level"]: normalize_wilayah_value(m["value"])}
            entity = enrich_hierarchy_from_index(entity)
 
        match_entities.append(entity)
 
    # Single msearch call instead of N sequential calls
    try:
        doc_counts = await get_entity_doc_counts_batch(es_client, match_entities)
    except Exception as e:
        logger.error("BATCH DOC COUNT ERROR: %s", e)
        doc_counts = [0] * len(possible_matches)
 
    for m, entity, doc_count in zip(possible_matches, match_entities, doc_counts):
        m["doc_count"] = doc_count
        if not m.get("hierarchy") or len(m.get("hierarchy", {})) <= 1:
            m["hierarchy"] = entity
    # =====================================================
    # DESA DOC_COUNT PENALTY (hindari desa generik/noise)
    # =====================================================
    for m in possible_matches:
        if m["level"] == "desa_kelurahan" and m.get("doc_count", 0) < 10:
            m["doc_count_penalty"] = True  # flag (optional)
            
    logger.debug(
        "DOC COUNTS: %s",
        [(m["value"], m.get("doc_count", 0)) for m in possible_matches]
    )
 
    # =====================================================
    # EARLY EXIT: Jika ada satu match kabupaten/kota dengan
    # doc_count >> semua kandidat lain → langsung resolve
    # tanpa perlu masuk ke ranking phase yang mahal.
    # Contoh: "kuningan jawa barat" → kab. kuningan (2413 docs)
    # vs kec. kuningan (228) vs desa kuningan (7/43/17)
    # =====================================================
    if not requested_level:
        kab_candidates = [
            m for m in possible_matches
            if m["level"] in ("kabupaten", "kota")
        ]
        if len(kab_candidates) == 1:
            winner = kab_candidates[0]
            winner_doc = winner.get("doc_count", 0)
            others_max = max(
                (m.get("doc_count", 0) for m in possible_matches if m is not winner),
                default=0
            )
            # Dominan jika doc_count winner > 5x kandidat lain
            if winner_doc > 0 and (others_max == 0 or winner_doc / max(others_max, 1) >= 5):
                entity = {winner["level"]: normalize_wilayah_value(winner["value"])}
                entity = enrich_hierarchy_from_index(entity)
                entity = attach_status(entity, status_sekolah)
                logger.debug(
                    "[EARLY_EXIT] Single dominant kab/kota: %s (doc=%d vs max_other=%d)",
                    winner["value"], winner_doc, others_max
                )
                return build_result(
                    status=ResultStatus.SUCCESS,
                    data={"entities": entity},
                    metadata={"auto_resolved_cross_level": True, "low_confidence_desa": False}
                )
 
 
    # =====================================================
    # REMOVE PARTIAL TOKEN COLLISION (GLOBAL CLEANING)
    # =====================================================
 
    multi_word_values = [
        normalize_query(_strip_loc_prefix(m["value"]))
        for m in possible_matches
        if len(
            normalize_query(_strip_loc_prefix(m["value"])).split()
        ) > 1
    ]
 
    cleaned_matches = []
 
    for m in possible_matches:
 
        stripped = normalize_query(_strip_loc_prefix(m["value"]))
 
        if (
            len(stripped.split()) == 1
            and any(
                re.search(r"\b" + re.escape(stripped) + r"\b", mw)
                for mw in multi_word_values
            )
        ):
            continue
 
        cleaned_matches.append(m)
 
    possible_matches = cleaned_matches
 
    if not possible_matches:
        # ── LANJUT KE FUZZY FALLBACK ─────────────────────────
 
        fuzzy_candidates = []
        reference_locations = build_reference_locations()
 
        normalized_query = normalize_query(cleaned)
 
        for ref in reference_locations:
            if requested_level and ref["level"] != requested_level:
                continue
 
            # Gunakan hanya fuzz.ratio (bukan partial_ratio) untuk akurasi
            # partial_ratio terlalu permisif → "jakarta" match ke nama acak
            score = fuzz.ratio(normalized_query, ref["normalized_name"])
 
            # Bonus: jika query adalah substring dari nama → tambah score
            if normalized_query in ref["normalized_name"]:
                score = max(score, 85)
 
            if score >= 90:   # ← naikkan threshold dari 87 ke 90
                fuzzy_candidates.append({
                    "value": ref["value"],
                    "level": ref["level"],
                    "score": score
                })
 
        fuzzy_candidates.sort(key=lambda x: x["score"], reverse=True)
 
        if fuzzy_candidates:
 
            return build_result(
                status=ResultStatus.AMBIGUOUS,
                data={
                    "candidates": [
                        build_candidate(c)
                        for c in fuzzy_candidates[:MAX_FUZZY_CANDIDATES]
                    ],
                    "suggestion_mode": "fuzzy_fallback"
                },
                metadata={"fuzzy_triggered": True}
            )
 
        return build_result(
            status=ResultStatus.NOT_FOUND,
            data={},
            metadata={}
        )
 
    max_doc = max((m.get("doc_count", 0) for m in possible_matches), default=0)
    max_scaled = math.log(max_doc + 1)
 
    if max_scaled == 0:
        for m in possible_matches:
            m["data_strength_score"] = 0
    else:
        for m in possible_matches:
            raw = m.get("doc_count", 0)
            scaled = math.log(raw + 1)
            normalized = scaled / max_scaled
            m["data_strength_score"] = round(normalized, 4)
    
    # =====================================================
    # DESA + KECAMATAN CONTEXT RESOLUTION (FIX 3)
    # Jalankan SEBELUM cross-level check agar "cibunar parungpanjang"
    # bisa resolve ke desa cibunar di kec. parungpanjang
    # =====================================================
    if not requested_level:
        desa_items = [m for m in possible_matches if m["level"] == "desa_kelurahan"]
        kec_items  = [m for m in possible_matches if m["level"] == "kecamatan"]
 
        if desa_items and kec_items:
            # Enrich hierarchy desa yang belum di-enrich
            for d in desa_items:
                if not d.get("hierarchy"):
                    entity_tmp = {d["level"]: normalize_wilayah_value(d["value"])}
                    d["hierarchy"] = enrich_hierarchy_from_index(entity_tmp)
 
            kec_values = {normalize_wilayah_value(k["value"]) for k in kec_items}
 
            # FIX: normalisasi nama untuk perbandingan
            def _strip_prefix(v):
                return normalize_query(
                    re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", v)
                )
 
            kec_names = {_strip_prefix(k["value"]) for k in kec_items}
 
            matched_desa = []
            for d in desa_items:
                d_name = _strip_prefix(d["value"])
 
                # GUARD: skip jika nama desa sama dengan nama kecamatan
                # Contoh: desa "parungpanjang" + kec. "parungpanjang"
                # → ini adalah kasus kec=desa yang sama nama, bukan 2 entitas berbeda
                if d_name in kec_names:
                    logger.debug(
                        "[DESA+KEC CONTEXT] Skip: desa '%s' sama nama dengan kecamatan",
                        d_name
                    )
                    continue
 
                d_hier = d.get("hierarchy", {})
                d_kec  = normalize_wilayah_value(d_hier.get("kecamatan", ""))
                if d_kec in kec_values:
                    matched_desa.append(d)
 
            if len(matched_desa) == 1:
                best = matched_desa[0]
                # Gunakan hierarchy asli dari find_all_location_matches
                # JANGAN enrich ulang karena akan return entries[0] yang salah
                if best.get("hierarchy") and len(best["hierarchy"]) > 1:
                    entity = dict(best["hierarchy"])
                else:
                    entity = {best["level"]: normalize_wilayah_value(best["value"])}
                    entity = enrich_hierarchy_from_index(entity)
                entity = attach_status(entity, status_sekolah)
                logger.debug(
                    "[DESA+KEC CONTEXT] Resolved: %s → %s",
                    best["value"], entity
                )
                return build_result(
                    status=ResultStatus.SUCCESS,
                    data={"entities": entity},
                    metadata={"auto_resolved_desa_kec_context": True}
                )
 
            elif len(matched_desa) > 1:
                return build_result(
                    status=ResultStatus.AMBIGUOUS,
                    data={"candidates": [build_candidate(d) for d in matched_desa]},
                    metadata={"reason": "multiple_desa_in_kecamatan"}
                )
 
    # =====================================================
    # SAME NAME AMBIGUITY CHECK (FUTURE SAFE VERSION)
    # =====================================================
 
    requested_level = extract_requested_level(cleaned)
 
    name_groups = {}
 
    for m in possible_matches:
        stripped = normalize_query(
            re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", m["value"])
        )
        name_groups.setdefault(stripped, []).append(m)
 
    # =====================================================
    # HANYA CEK CROSS-LEVEL JIKA USER TIDAK SEBUT LEVEL
    # =====================================================
    if not requested_level:
        for name, items in name_groups.items():
            levels = set(i["level"] for i in items)
            if len(items) == 1 and len(levels) == 1:
                continue
            if len(levels) > 1:
                level_priority = {"provinsi": 4, "kabupaten": 3, "kota": 3,
                                "kecamatan": 2, "desa_kelurahan": 1}
                sorted_items = sorted(
                    items,
                    key=lambda x: (
                        level_priority.get(x["level"], 0),
                        x.get("doc_count", 0)
                    ),
                    reverse=True
                )
                top = sorted_items[0]
                second = sorted_items[1] if len(sorted_items) > 1 else None
 
                top_priority = level_priority.get(top["level"], 0)
                second_priority = level_priority.get(second["level"], 0) if second else 0
 
                # Auto-resolve jika level tertinggi berbeda level
                if top_priority > second_priority:
                    best = top               
                    entity = {best["level"]: normalize_wilayah_value(best["value"])}
                    entity = enrich_hierarchy_from_index(entity)
                    entity = attach_status(entity, status_sekolah)
 
                    low_confidence = (
                        best["level"] == "desa_kelurahan"
                        and best.get("doc_count", 0) < 20
                        and not requested_level
                    )
 
                    return build_result(
                        status=ResultStatus.SUCCESS,
                        data={"entities": entity},
                        metadata={
                            "auto_resolved_cross_level": True,  
                            "low_confidence_desa": low_confidence
                        }
                    )
 
                # Level sama (kab vs kab, atau kota vs kab) → filter dulu
                # Eliminasi level rendah yang doc_count << level tinggi
                top_doc = top.get("doc_count", 0)
                filtered_items = []
                for item in sorted_items:
                    item_priority = level_priority.get(item["level"], 0)
                    item_doc = item.get("doc_count", 0)
                    # Pertahankan jika level sama dengan top atau doc_count cukup besar
                    if item_priority == top_priority:
                        filtered_items.append(item)
                    elif top_doc > 0 and item_doc / top_doc > 0.05:
                        # Hanya 5% dari top → skip (noise)
                        filtered_items.append(item)
                    else:
                        logger.debug(
                            "[CROSS-LEVEL] Eliminated '%s' (doc=%d, ratio=%.3f)",
                            item["value"], item_doc,
                            item_doc / top_doc if top_doc > 0 else 0
                        )
 
                if len(filtered_items) == 1:
                    best = filtered_items[0]
                    entity = {best["level"]: normalize_wilayah_value(best["value"])}
                    entity = enrich_hierarchy_from_index(entity)
                    entity = attach_status(entity, status_sekolah)
                    return build_result(
                        status=ResultStatus.SUCCESS,
                        data={"entities": entity},
                        metadata={"auto_resolved_dominant": True}
                    )
 
                return build_result(
                    status=ResultStatus.AMBIGUOUS,
                    data={"candidates": [build_candidate(i) for i in filtered_items]},
                    metadata={"reason": "cross_level_same_name"}
                )
 
    # =====================================================
    # FILTER BY EXPLICIT LEVEL (FIX MULTI SAFE)
    # =====================================================
    if requested_level:
 
        if requested_level == "kota":
            filtered = [
                m for m in possible_matches
                if m["level"] in ["kota", "kabupaten"]
                and m["value"].lower().startswith("kota")
            ]
 
        elif requested_level == "kabupaten":
            filtered = [
                m for m in possible_matches
                if m["level"] == "kabupaten"
                and normalize_wilayah_value(m["value"]).startswith("kab.")
            ]
 
        else:
            filtered = [
                m for m in possible_matches
                if m["level"] == requested_level
            ]
 
        # =======================================================
        # NEW: CONTEXT-AWARE FILTER UNTUK DESA LEVEL
        # Jika user menyebutkan kabupaten/provinsi konteks,
        # gunakan untuk memilih kandidat desa yang tepat.
        # Contoh: "desa cibunar bogor" → filter cibunar di Kab. Bogor
        # =======================================================
        if requested_level == "desa_kelurahan" and len(filtered) > 1:
            kab_context  = _extract_kabupaten_context(cleaned)
            prov_context = _extract_provinsi_context(cleaned)
 
            if kab_context or prov_context:
                logger.info(
                    "[DESA CONTEXT] Applying context filter: kab=%s prov=%s",
                    kab_context, prov_context
                )
                filtered = _filter_desa_by_context(filtered, kab_context, prov_context)
 
        logger.debug(
            "FILTERED BY EXPLICIT LEVEL (%s): %s",
            requested_level,
            filtered
        )
 
        if len(filtered) == 0:
            return build_result(
                status=ResultStatus.NOT_FOUND,
                data={},
                metadata={"reason": "level_not_found"}
            )
 
        if len(filtered) == 1:
 
            best = filtered[0]
 
            entity = {
                best["level"]: normalize_wilayah_value(best["value"])
            }
 
            entity = enrich_hierarchy_from_index(entity)
            entity = attach_status(entity, status_sekolah)
 
            return build_result(
                status=ResultStatus.SUCCESS,
                data={"entities": entity},
                metadata={"explicit_single_match": True}
            )
 
        # Masih > 1 setelah context filter → ambiguous
        if len(filtered) > 1:
 
            # Cek apakah ini multi-entity request (pakai "dan"/"&")
            if any(sep in cleaned for sep in [" dan ", ",", "&"]):
                entities_list = []
                for match in filtered:
                    entity = {
                        match["level"]: normalize_wilayah_value(match["value"])
                    }
                    entity = enrich_hierarchy_from_index(entity)
                    entities_list.append(entity)
 
                return build_result(
                    status=ResultStatus.SUCCESS,
                    data={
                        "entities": entities_list,
                        "is_multi": True
                    },
                    metadata={"explicit_multi_match": True}
                )
 
            # Single query tapi masih ambiguous setelah context filter
            return build_result(
                status=ResultStatus.AMBIGUOUS,
                data={
                    "candidates": [
                        build_candidate(m)
                        for m in filtered
                    ]
                },
                metadata={"reason": "desa_still_ambiguous_after_context"}
            )
    # Safety: jika possible_matches kosong setelah semua filter → NOT_FOUND
    if not possible_matches:
        return build_result(
            status=ResultStatus.NOT_FOUND,
            data={},
            metadata={"reason": "no_matches_after_filtering"}
        )
 
    # Inisialisasi top_level_matches sebagai fallback
    top_level_matches = possible_matches[:1]  # minimal 1 item
    # =====================================================
    # SMART RANKING SCORE
    # =====================================================
 
    for m in possible_matches:
        m["ranking_score"] = compute_entity_ranking_score(m, cleaned)
 
    possible_matches.sort(
        key=lambda x: (
            x["ranking_score"],
            x.get("data_strength_score", 0),
            LEVEL_PRIORITY.get(x["level"], 0)
        ),
        reverse=True
    )
 
    logger.debug(
        "RANKING SCORES: %s",
        [
            (
                m["value"],
                m["level"],
                round(m["ranking_score"], 3),
                m.get("doc_count", 0)
            )
            for m in possible_matches
        ]
    )
 
    top_score = possible_matches[0]["ranking_score"]
    second_score = (
        possible_matches[1]["ranking_score"]
        if len(possible_matches) > 1 else 0
    )
 
    ranking_gap = top_score - second_score
    ranking_gap = round(ranking_gap, 4)
 
    top_level_matches = [
        m for m in possible_matches
        if abs(m["ranking_score"] - top_score) < 0.05
    ]
 
    # =========================================
    # DOMINANCE CHECK
    # =========================================
 
    top_doc = possible_matches[0].get("doc_count", 0)
    second_doc = (
        possible_matches[1].get("doc_count", 0)
        if len(possible_matches) > 1 else 0
    )
 
    dominance_ratio = (
        top_doc / second_doc
        if second_doc > 0 else float("inf")
    )
 
    # =====================================================
    # EXACT MATCH CHECK (ONLY IN TOP LEVEL)
    # =====================================================
    exact_matches = []
 
    for item in top_level_matches:
 
        value_clean = normalize_query(
            re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", item["value"])
        )
 
        if re.search(r"\b" + re.escape(value_clean) + r"\b", cleaned):
            exact_matches.append(item)
 
    # =====================================================
    # SINGLE EXACT MATCH → RESOLVE
    # =====================================================
    if len(exact_matches) == 1:
 
        best = exact_matches[0]
 
        entity = {
            best["level"]: normalize_wilayah_value(best["value"])
        }
 
        entity = enrich_hierarchy_from_index(entity)
        entity = attach_status(entity, status_sekolah)
 
        # TAMBAHAN: low confidence detection untuk desa kecil
        low_confidence = (
            best["level"] == "desa_kelurahan"
            and best.get("doc_count", 0) < 20
            and not requested_level
        )
 
        return build_result(
            status=ResultStatus.SUCCESS,
            data={"entities": entity},
            metadata={
                "auto_disambiguated": True,
                "low_confidence_desa": low_confidence
            }
        )
 
    # =====================================================
    # MULTIPLE MATCHES → AMBIGUOUS
    # =====================================================
    DOMINANCE_THRESHOLD = 2.5
    RANKING_GAP_THRESHOLD = 0.04
 
    if (
        ranking_gap < RANKING_GAP_THRESHOLD
        and dominance_ratio < DOMINANCE_THRESHOLD
    ):
        sorted_candidates = sorted(
            possible_matches[:5],
            key=lambda x: (
                x["ranking_score"],
                x.get("doc_count", 0)
            ),
            reverse=True
        )
        return build_result(
            status=ResultStatus.AMBIGUOUS,
            data={
                "candidates": [
                    build_candidate(m)
                    for m in sorted_candidates
                ]
            },
            metadata={
                "ambiguous": True,
                "ranking_gap": ranking_gap,
                "dominance_ratio": round(dominance_ratio, 3)
            }
        )
 
    # =====================================================
    # SINGLE TOP LEVEL MATCH → RESOLVE
    # =====================================================
    best = top_level_matches[0]
    entity = {best["level"]: normalize_wilayah_value(best["value"])}
    entity = enrich_hierarchy_from_index(entity)
    entity = attach_status(entity, status_sekolah)
 
    # Flag jika desa di-resolve dari satu entry tanpa konfirmasi user
    low_confidence = (
        best["level"] == "desa_kelurahan"
        and best.get("doc_count", 0) < 20
        and not requested_level  # user tidak sebut "desa"
    )
 
    return build_result(
        status=ResultStatus.SUCCESS,
        data={"entities": entity},
        metadata={
            "auto_resolved_by_priority": True,
            "ranking_gap": ranking_gap,
            "low_confidence_desa": low_confidence
        }
    )
