# list_school_handler.py
 
import re
import logging
from typing import Dict, List, Optional, Tuple, Set
from elasticsearch import AsyncElasticsearch
from app.services.core.es_query_builder import extract_filters_from_intent

logger = logging.getLogger(__name__)
 
# PAUD adalah kategori umbrella — expand ke jenjang konkret di ES
_PAUD_BENTUK_EXPAND = ["TK", "KB", "RA", "SPS", "TPA", "PAUDQ", "SPK TK", "SPK KB", "SDTK"]
 
_AMBIGUOUS_SINGLE_WORDS = {
    "timur", "barat", "utara", "selatan", "tengah",
    "dalam", "luar", "baru", "lama", "raya", "induk",
}
 
_NON_STANDALONE_LOCATION_WORDS = {
    "barat", "timur", "utara", "selatan", "tengah",
    "dalam", "luar", "baru", "lama", "raya",
}
 
# ================================================================
# SORT KEYWORDS — deteksi niat pengurutan dari query
# ================================================================
_SORT_QUALITY_KEYWORDS = {
    "terbaik", "unggulan", "favorit", "terfavorit", "top", "berkualitas",
    "terakreditasi a", "akreditasi a", "terbaik akreditasi",
}
_SORT_SIZE_KEYWORDS = {
    "terbesar", "terbanyak siswa", "paling banyak siswa",
    "jumlah siswa terbanyak", "terbanyak murid",
}
_SORT_GURU_KEYWORDS = {
    "guru terbanyak", "paling banyak guru", "jumlah guru terbanyak",
    "tenaga pengajar terbanyak",
}

def _detect_sort_mode(query: str) -> str:
    q = query.lower()
    if any(kw in q for kw in _SORT_QUALITY_KEYWORDS):
        return "quality"
    if any(kw in q for kw in _SORT_GURU_KEYWORDS):
        return "guru"          # ← mode baru
    if any(kw in q for kw in _SORT_SIZE_KEYWORDS):
        return "size"
    return "default"

_AKREDITASI_ORDER_SCRIPT = (
    "def v = doc.containsKey('akreditasi') && doc['akreditasi'].size() > 0"
    " ? doc['akreditasi'].value : 'Z';"
    "if (v == 'A') { return 1; }"
    "if (v == 'B') { return 2; }"
    "if (v == 'C') { return 3; }"
    "return 4;"
)
 
def _build_sort_clause(sort_mode: str) -> List[Dict]:
    if sort_mode == "quality":
        # Akreditasi terbaik (A→B→C) sebagai sort primer, relevansi sebagai tiebreaker
        return [
            {"_script": {"type": "number",
                "script": {"lang": "painless", "source": _AKREDITASI_ORDER_SCRIPT},
                "order": "asc"}},
            {"_score": {"order": "desc"}},
        ]
    if sort_mode == "size":
        # Urutkan dari siswa terbanyak — field siswa_total dari es_structure.md
        return [
            {"siswa_total": {"order": "desc", "missing": "_last"}},
            {"_score": {"order": "desc"}},
        ]
    if sort_mode == "guru":
        # Urutkan dari guru terbanyak — field guru_total dari es_structure.md
        return [
            {"guru_total": {"order": "desc", "missing": "_last"}},
            {"_score": {"order": "desc"}},
        ]
    return [{"_score": {"order": "desc"}}]
 
# ================================================================
# OPTIONAL AGGREGATIONS FOR BREAKDOWN (jenjang + status)
# ================================================================
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
# ================================================================
# FUZZY MATCH
# ================================================================
 
def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]
 
 
def _fuzzy_match_keyword(keyword: str) -> Optional[Tuple[str, str]]:
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        return None
 
    kw = keyword.lower().strip()
    if len(kw) < 4 or " " in kw:
        return None
 
    threshold = min(2, max(1, len(kw) // 4))
    best_match = None
    best_dist  = 999
 
    for level in ["kecamatan", "kabupaten", "provinsi"]:
        lookup = LOCATION_INDEX.get(level, {})
        for key in lookup:
            key_clean = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key).strip()
            key_norm  = re.sub(r"[^\w\s]", " ", key_clean.lower()).strip()
            if not key_norm or len(key_norm) < 3:
                continue
            if len(key_norm.split()) != 1:
                continue
            dist = _levenshtein(kw, key_norm)
            if dist <= threshold and dist < best_dist:
                best_dist  = dist
                best_match = (key_norm, level)
 
    if best_match:
        logger.info("[LIST] Fuzzy match: '%s' → '%s' (level=%s dist=%d)",
                    keyword, best_match[0], best_match[1], best_dist)
    return best_match
 
 
def _apply_typo_correction(kw: str, q_low: str = "") -> Optional[Tuple[str, str]]:
    if not kw or len(kw) < 4 or " " in kw:
        return None
    return _fuzzy_match_keyword(kw)
 
 
# ================================================================
# MULTI-KATA LOCATION CHECK
# Cek apakah beberapa token membentuk satu nama lokasi multi-kata.
# Digunakan sebagai guard SEBELUM implicit split.
# Juga digunakan di handle() untuk mendeteksi wilayah multi-kata
# dan langsung meneruskannya tanpa fallback.
# ================================================================
 
def _is_multikata_location(tokens: List[str]) -> Optional[Tuple[str, str]]:
    """
    Cek apakah token-token membentuk satu nama lokasi multi-kata.
    Return (nama_lower, level) atau None.
    Prioritas: provinsi → kabupaten → kecamatan (dari panjang ke pendek).
    """
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        return None
 
    # Cek dari level tertinggi dulu agar "papua barat" → provinsi, bukan kab
    for level in ["provinsi", "kabupaten", "kecamatan", "desa_kelurahan"]:
        lookup = LOCATION_INDEX.get(level, {})
        for n in range(min(4, len(tokens)), 1, -1):
            for i in range(len(tokens) - n + 1):
                phrase = " ".join(tokens[i:i+n])
                phrase_lower = phrase.lower()
                if (phrase_lower in lookup
                        or f"kec. {phrase_lower}" in lookup
                        or f"kab. {phrase_lower}" in lookup
                        or f"kota {phrase_lower}" in lookup
                        or f"prov. {phrase_lower}" in lookup):
                    logger.info("[LIST] Multi-kata location found: '%s' level=%s", phrase_lower, level)
                    return (phrase_lower, level)
 
    return None
 
def _is_multikata_with_context(tokens: List[str]) -> Optional[Tuple[Tuple[str, str], Optional[Dict]]]:
    """
    Cek apakah token-token mengandung nama multi-kata PLUS token kecamatan/kabupaten.
    
    Prioritas deteksi multi-kata utama: desa → kecamatan → kabupaten → provinsi
    (kebalikan dari _is_multikata_location yang prioritas provinsi dulu,
    karena di sini kita ingin filter desa yang paling spesifik)
    
    Sisa token dicek sebagai kecamatan/kabupaten (bisa multi-kata).
    """
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        return None
 
    kec_lookup = LOCATION_INDEX.get("kecamatan", {})
    kab_lookup = LOCATION_INDEX.get("kabupaten", {})
 
    # Coba level desa dulu, kemudian kecamatan, kabupaten, provinsi
    # Ini berbeda dari _is_multikata_location yang provinsi dulu
    for level in ["desa_kelurahan", "kecamatan", "kabupaten", "provinsi"]:
        lookup = LOCATION_INDEX.get(level, {})
        for n in range(min(4, len(tokens)), 1, -1):
            for i in range(len(tokens) - n + 1):
                phrase = " ".join(tokens[i:i+n])
                phrase_lower = phrase.lower()
                if (phrase_lower in lookup
                        or f"kec. {phrase_lower}" in lookup
                        or f"kab. {phrase_lower}" in lookup
                        or f"kota {phrase_lower}" in lookup
                        or f"prov. {phrase_lower}" in lookup):
 
                    # Token sisa (di luar range i..i+n)
                    remaining = tokens[:i] + tokens[i+n:]
 
                    if not remaining:
                        return ((phrase_lower, level), None)
 
                    # Cek sisa token sebagai kecamatan/kabupaten
                    # Coba dari panjang ke pendek (multi-kata juga)
                    for m in range(min(3, len(remaining)), 0, -1):
                        for j in range(len(remaining) - m + 1):
                            rem_phrase = " ".join(remaining[j:j+m])
                            rem_lower  = rem_phrase.lower()
 
                            # Skip kata ambigu standalone
                            if m == 1 and rem_lower in _NON_STANDALONE_LOCATION_WORDS:
                                continue
 
                            rem_in_kec = (rem_lower in kec_lookup
                                          or f"kec. {rem_lower}" in kec_lookup)
                            rem_in_kab = (rem_lower in kab_lookup
                                          or f"kab. {rem_lower}" in kab_lookup
                                          or f"kota {rem_lower}" in kab_lookup)
 
                            if rem_in_kec or rem_in_kab:
                                extra = {}
                                if level == "desa_kelurahan":
                                    extra["desa"] = phrase_lower
                                    if rem_in_kec and not rem_in_kab:
                                        extra["kecamatan"] = rem_lower
                                    elif rem_in_kab and not rem_in_kec:
                                        extra["kabupaten"] = rem_lower
                                    else:
                                        # Ada di keduanya — kecamatan lebih spesifik
                                        extra["kecamatan"] = rem_lower
                                logger.info(
                                    "[LIST] Multi-kata+context: '%s'(%s) + '%s'",
                                    phrase_lower, level, rem_lower
                                )
                                return ((phrase_lower, level), extra if extra else None)
 
                    # Sisa token ada tapi tidak cocok di kec/kab
                    return ((phrase_lower, level), None)
 
    return None
 
# ================================================================
# FIX 2: _has_province_siblings — baru, untuk deteksi provinsi
# dengan "saudara" seperti papua/papua barat/papua selatan.
# Jika nama provinsi X punya sibling provinsi yang mengandung X,
# maka query "di X" harus trigger clarification.
# ================================================================
def _has_province_siblings(name: str) -> bool:
    """
    Cek apakah ada provinsi lain yang namanya mengandung `name`
    sebagai prefix/bagian. Contoh: "papua" → ada "papua barat",
    "papua selatan" → return True → perlu clarification.
    """
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        return False
 
    prov_lookup = LOCATION_INDEX.get("provinsi", {})
    name_lower  = name.lower().strip()
    count = 0
    for key in prov_lookup:
        key_clean = re.sub(r"^prov\.\s*", "", key).strip().lower()
        if key_clean == name_lower:
            count += 1
        elif key_clean.startswith(name_lower + " ") or key_clean.startswith(name_lower + "-"):
            count += 1
    # Jika lebih dari satu provinsi yang match (diri sendiri + sibling)
    return count > 1
 
# ================================================================
# IMPLICIT MULTI-WILAYAH
# ================================================================
 
def _detect_implicit_multi_wilayah(query: str) -> Optional[Dict]:
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        return None
 
    stopwords = {
        "daftar", "sekolah", "di", "ada", "cari", "list",
        "smp", "sma", "sd", "smk", "tk", "mi", "mts", "ma",
        "man", "mtsn", "min", "negeri", "swasta",
    }
 
    q = re.sub(r"[^\w\s]", " ", query.lower()).strip()
    tokens = [t for t in q.split() if t not in stopwords and len(t) >= 3]
 
    if len(tokens) < 2:
        return None
 
    # Guard: jika token-token membentuk nama multi-kata valid, jangan split
    multikata = _is_multikata_location(tokens)
    if multikata:
        logger.info("[LIST] Tokens form a multi-kata location '%s', skip implicit split", multikata[0])
        return None
 
    desa_lookup = LOCATION_INDEX.get("desa_kelurahan", {})
    kec_lookup  = LOCATION_INDEX.get("kecamatan", {})
    kab_lookup  = LOCATION_INDEX.get("kabupaten", {})
 
    for i in range(len(tokens)):
        for j in range(len(tokens)):
            if i == j:
                continue
            t1 = tokens[i]
            t2 = tokens[j]
 
            if t2 in _NON_STANDALONE_LOCATION_WORDS:
                continue
 
            t1_in_desa = (t1 in desa_lookup or f"desa {t1}" in desa_lookup)
            t2_in_kec  = (t2 in kec_lookup or f"kec. {t2}" in kec_lookup)
            t2_in_kab  = (t2 in kab_lookup or f"kab. {t2}" in kab_lookup
                          or f"kota {t2}" in kab_lookup)
 
            if t1_in_desa and (t2_in_kec or t2_in_kab):
                result = {"desa": t1}
                if t2_in_kec and not t2_in_kab:
                    result["kecamatan"] = t2
                elif t2_in_kab and not t2_in_kec:
                    result["kabupaten"] = t2
                else:
                    result["kecamatan"] = t2
                logger.info("[LIST] Implicit multi-wilayah detected: %s", result)
                return result
 
    return None
 
 
 
 
# ================================================================
# SPLIT QUERY "DAN" — deteksi dua lokasi dipisahkan "dan/serta"
# ================================================================
def _split_dan_query(query: str) -> Optional[List[str]]:
    """
    Deteksi pola dua lokasi dipisah "dan"/"serta".
    Contoh: "cibunar parungpanjang dan lumpang parungpanjang"
            → ["paud di cibunar parungpanjang", "paud di lumpang parungpanjang"]
    """
    # GUARD: jangan split jika "dan" ada di antara dua token akreditasi/jenjang
    _AKRED_TOKENS = {"terakreditasi", "akreditasi", "tidak terakreditasi"}
    q_low = query.lower()
    # Deteksi pola: "terakreditasi X dan terakreditasi Y"
    if re.search(r'terakreditasi\s+\w+\s+dan\s+terakreditasi', q_low):
        return None
    q_clean = re.sub(r"[^\w\s]", " ", query.lower()).strip()
    # Split pada berbagai kata penghubung
    _SPLITTERS = r"\b(dan|serta|atau|dengan|beserta|maupun|juga|ataupun|sama)\b"
    parts = re.split(_SPLITTERS, q_clean)
    chunks = [p.strip() for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]
    if len(chunks) != 2:
        return None
    sw = {"di", "daftar", "sekolah", "paud", "sd", "smp", "sma",
          "smk", "tk", "ra", "kb", "dan", "serta", "atau"}
    def has_loc_token(chunk: str) -> bool:
        return any(len(t) >= 3 and t not in sw for t in chunk.split())
    if not (has_loc_token(chunks[0]) and has_loc_token(chunks[1])):
        return None
    # Guard: jangan split jika ada kata filter di salah satu chunk
    # (mis. "paud dengan akreditasi A di kuningan")
    _FILTER_WORDS = {"akreditasi", "negeri", "swasta", "status", "jumlah",
                     "terbanyak", "urutkan", "minimal", "maksimal"}
    for chunk in chunks:
        if any(fw in chunk.split() for fw in _FILTER_WORDS):
            return None
    # Ambil prefix jenjang/kata sebelum nama lokasi pertama
    prefix_match = re.match(
        r"^(.*?\bdi\b\s*)", query.lower()
    )
    prefix = prefix_match.group(1) if prefix_match else ""
    # Bersihkan chunk dari prefix yang sudah ada (hindari "paud di paud di ...")
    def _clean_chunk(chunk: str) -> str:
        p = prefix.strip()
        c = chunk.strip()
        if p and c.startswith(p):
            c = c[len(p):].strip()
        return c
    return [prefix + _clean_chunk(chunks[0]), prefix + _clean_chunk(chunks[1])]
 
# ================================================================
# MULTI-WILAYAH EXTRACTION (explicit dengan hint level)
# ================================================================
 
# Kata-kata yang menandakan nama wilayah sudah selesai
_FILTER_BOUNDARY_WORDS = {
    "yang", "akreditasi", "jumlah", "siswa", "murid", "urutkan",
    "dengan", "diatas", "di atas", "dibawah", "di bawah", "atas",
    "negeri", "swasta", "terbanyak", "terbesar", "terkecil",
    "paling", "minimal", "maksimal", "lebih", "kurang",
    "dan", "atau", "serta",
}
 
def _extract_multi_wilayah(query: str) -> Optional[Dict]:
    q = query.lower().strip()
    q = re.sub(r"[^\w\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
 
    level_patterns = [
        ("desa",      r"\b(desa|kelurahan)\b"),
        ("kecamatan", r"\b(kecamatan|kec)\b"),
        ("kabupaten", r"\b(kabupaten|kab)\b"),
        ("kota",      r"\bkota\b"),
        ("provinsi",  r"\b(provinsi|prov)\b"),
    ]
 
    found = []
    for canonical, pattern in level_patterns:
        for m in re.finditer(pattern, q):
            found.append((m.start(), m.end(), canonical))
 
    if len(found) < 2:
        return None
 
    found.sort(key=lambda x: x[0])
 
    result = {}
    for i, (start, end, canonical) in enumerate(found):
        name_start = end
        name_end   = found[i + 1][0] if i + 1 < len(found) else len(q)
        name = q[name_start:name_end].strip()
        name = re.sub(r"^\b(di|dan)\b\s*", "", name).strip()
        name = re.sub(r"\b(daftar|sekolah|di|ada|cari|list)\b", "", name).strip()
        name = re.sub(r"\s+", " ", name).strip()
 
        # === PERBAIKAN: potong nama saat menemukan kata filter ===
        tokens = name.split()
        clean_tokens = []
        for tok in tokens:
            if tok in _FILTER_BOUNDARY_WORDS:
                break
            clean_tokens.append(tok)
        name = " ".join(clean_tokens).strip()
        # =========================================================
 
        if name and len(name) >= 3:
            result[canonical] = name
 
    logger.info("[LIST] _extract_multi_wilayah raw result: %s", result)
    return result if len(result) >= 2 else None
 
 
# ================================================================
# LOCATION CANDIDATE LOOKUP
# ================================================================
 
def _find_location_candidates(keyword: str) -> List[Dict]:
    try:
        from app.services.location_service import LOCATION_HIERARCHY, LOCATION_INDEX
    except ImportError:
        return []
 
    if not keyword or len(keyword) < 3:
        return []
 
    kw      = keyword.lower().strip()
    pattern = re.compile(r"\b" + re.escape(kw) + r"\b")
    seen: Set[str] = set()
 
    by_level: Dict[str, List[Dict]] = {
        "provinsi": [], "kabupaten": [], "kecamatan": [], "desa_kelurahan": []
    }
 
    for level in ["desa_kelurahan", "kecamatan", "kabupaten"]:
        hier = LOCATION_HIERARCHY.get(level, {})
        for raw_key, entries in hier.items():
            key_clean = re.sub(
                r"^(prov\.\s*|kab\.\s*|kabupaten\s+|kota\s+|kec\.\s*|"
                r"kecamatan\s+|desa\s+|kelurahan\s+)", "", raw_key
            ).strip()
            if not pattern.search(key_clean):
                continue
            if isinstance(entries, dict):
                entries = [entries]
            elif not isinstance(entries, list):
                continue
            for meta in entries:
                if not isinstance(meta, dict):
                    continue
                kec  = meta.get("kecamatan", "") or ""
                kab  = meta.get("kabupaten",  "") or ""
                prov = meta.get("provinsi",   "") or ""
                dedup_key = f"{level}|{raw_key}|{kec}|{kab}".lower()
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                nama_raw = raw_key.title() if level == "kabupaten" else ""
                by_level[level].append({
                    "level": level, "nama": key_clean,
                    "nama_raw": nama_raw, "nama_display": key_clean,
                    "kecamatan": kec, "kabupaten": kab, "provinsi": prov,
                })
 
    try:
        prov_lookup = LOCATION_INDEX.get("provinsi", {})
        for pkey, praw in prov_lookup.items():
            pkey_clean = re.sub(r"^prov\.\s*", "", pkey).strip()
            if not pattern.search(pkey_clean):
                continue
            dedup_key = f"provinsi|{pkey_clean}".lower()
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            by_level["provinsi"].append({
                "level": "provinsi", "nama": pkey_clean,
                "nama_raw": praw, "nama_display": pkey_clean,
                "kecamatan": "", "kabupaten": "", "provinsi": praw,
            })
    except Exception:
        pass
 
    candidates  = by_level["provinsi"][:5]
    candidates += by_level["kabupaten"][:10]
    candidates += by_level["kecamatan"][:10]
    candidates += by_level["desa_kelurahan"][:15]
 
    logger.info("[LIST] Candidates for keyword=%s: %d (prov=%d kab=%d kec=%d desa=%d)",
                keyword, len(candidates),
                len(by_level["provinsi"]), len(by_level["kabupaten"]),
                len(by_level["kecamatan"]), len(by_level["desa_kelurahan"]))
 
    return candidates[:40]
 
 
# ================================================================
# FALLBACK WILAYAH DETECTION
# ================================================================
 
def _fallback_detect_wilayah(query: str) -> Optional[Tuple[str, str]]:
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        return None
 
    q = re.sub(r"[^\w\s]", " ", query.lower())
    q = re.sub(r"\s+", " ", q).strip()
 
    has_desa_hint = bool(re.search(r"\b(desa|kelurahan)\b", q))
    has_prov_hint = bool(re.search(r"\bprovinsi\b", q))
    has_kab_hint  = bool(re.search(r"\b(kabupaten|kab)\b", q))
    has_kec_hint  = bool(re.search(r"\b(kecamatan|kec)\b", q))
    has_kota_hint = bool(re.search(r"\bkota\b", q))
    has_explicit_hint = has_kec_hint or has_kab_hint or has_prov_hint or has_desa_hint or has_kota_hint
 
    if has_kec_hint:
        check_order = ["kecamatan"]
    elif has_kab_hint or has_kota_hint:
        check_order = ["kabupaten"]
    elif has_prov_hint:
        check_order = ["provinsi"]
    elif has_desa_hint:
        check_order = ["desa_kelurahan"]
    else:
        check_order = ["kecamatan", "kabupaten", "provinsi"]
 
    for level in check_order:
        lookup = LOCATION_INDEX.get(level, {})
        if not lookup:
            continue
        # FIX: sort by len(key_clean) DESC agar nama lebih panjang diperiksa duluan
        # "sri raharja" (10 char) harus ditemukan sebelum "raharja" (7 char)
        def key_clean_len(item):
            key = item[0]
            clean = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key).strip()
            return len(clean)
 
        sorted_keys = sorted(lookup.items(), key=key_clean_len, reverse=True)
 
        for key, raw in sorted_keys:
            key_clean = re.sub(r"^(prov\.|kab\.|kota|kec\.)\s*", "", key)
            key_norm  = re.sub(r"[^\w\s]", " ", key_clean.lower()).strip()
            if not key_norm:
                continue
            key_words = key_norm.split()
            if len(key_words) == 1 and key_norm in _AMBIGUOUS_SINGLE_WORDS and not has_explicit_hint:
                continue
            if re.search(r"\b" + re.escape(key_norm) + r"\b", q):
                logger.info("[LIST] Fallback detected: level=%s name=%s", level, key_norm)
                return (key_norm, level)
 
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
 
def _has_multi_level_match(keyword: str) -> bool:
    try:
        from app.services.location_service import LOCATION_HIERARCHY, LOCATION_INDEX
    except ImportError:
        return False
    kw      = keyword.lower().strip()
    pattern = re.compile(r"\b" + re.escape(kw) + r"\b")
    matched_levels = set()
    for level in ["desa_kelurahan", "kecamatan", "kabupaten"]:
        hier = LOCATION_HIERARCHY.get(level, {})
        for raw_key in hier:
            key_clean = re.sub(
                r"^(prov\.\s*|kab\.\s*|kabupaten\s+|kota\s+|kec\.\s*|"
                r"kecamatan\s+|desa\s+|kelurahan\s+)", "", raw_key
            ).strip()
            if pattern.search(key_clean):
                matched_levels.add(level)
                break
    try:
        prov_lookup = LOCATION_INDEX.get("provinsi", {})
        for pkey in prov_lookup:
            pkey_clean = re.sub(r"^prov\.\s*", "", pkey).strip()
            if pattern.search(pkey_clean):
                matched_levels.add("provinsi")
                break
    except Exception:
        pass
    result = len(matched_levels) >= 2
    if result:
        logger.info("[LIST] Multi-level match: keyword=%s levels=%s", keyword, matched_levels)
    return result
 
 
def _extract_location_keyword(query: str) -> str:
    stopwords = {
        "daftar", "sekolah", "di", "ada", "cari", "list", "semua",
        "data", "tentang", "sebuah",
        "smp", "sma", "sd", "smk", "tk", "mi", "mts", "ma",
        "man", "mtsn", "min", "negeri", "swasta",
        # Jenjang PAUD agar tidak jadi bagian keyword lokasi
        "paud", "sps", "tpa", "paudq", "sdtk",
        # Kata area generik
        "daerah", "wilayah", "area", "kawasan", "sekitar",
    }
    tokens = re.sub(r"[^\w\s]", " ", query.lower()).split()
    keywords = [t for t in tokens if t not in stopwords and len(t) >= 3]
    return " ".join(keywords).strip()
 
 
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

    return parsed

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

        dan_parts = None
        if _depth == 0 and not any(kw in query.lower() for kw in _GEO_KEYWORDS_GUARD):
            dan_parts = _split_dan_query(query)
        # ── Step 0: Deteksi dua lokasi dipisah "dan/serta" ────────
        dan_parts = _split_dan_query(query) if _depth == 0 else None
        if dan_parts and len(dan_parts) == 2:
            logger.info("[LIST] Dan-split detected: %s", dan_parts)
            result_a = await self.handle(dan_parts[0], intent=intent, filters=filters, _depth=1)
            result_b = await self.handle(dan_parts[1], intent=intent, filters=filters, _depth=1)
            if (result_a.get("status") == "success"
                    and result_b.get("status") == "success"):
                merged_schools = result_a["schools"] + result_b["schools"]
                merged_total   = result_a["total"] + result_b["total"]
                wilayah_a = result_a.get("parsed", {}).get("wilayah", "")
                wilayah_b = result_b.get("parsed", {}).get("wilayah", "")
                merged_parsed  = dict(result_a.get("parsed", {}))
                merged_parsed["wilayah"] = f"{wilayah_a} dan {wilayah_b}"
                logger.info("[LIST] Dan-split merged: %d + %d = %d schools",
                            result_a["total"], result_b["total"], merged_total)
                return {
                    "status": "success", "intent": "school_list",
                    "total": merged_total, "schools": merged_schools,
                    "parsed": merged_parsed,
                    "wilayah_level": result_a.get("wilayah_level", "desa_kelurahan"),
                    "is_ambiguous": False, "candidates": [],
                    "breakdown_jenjang": None,
                    "breakdown_status":  None,
                }
            # Salah satu gagal → lanjut flow normal
 
        # ── Step 1: Multi-wilayah eksplisit ──────────────────────
        multi = _extract_multi_wilayah(query)
        if multi and len(multi) >= 2:
            logger.info("[LIST] Multi-wilayah detected: %s", multi)
            logger.info("[LIST] parsed akreditasi=%s", parsed.get("akreditasi"))
            parsed = _inject_intent_filters(parsed, intent)
            return await self._handle_multi_wilayah(query, parsed, multi)
 
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
                        return await self._handle_multi_wilayah(query, parsed, mk_extra)
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
                return await self._handle_multi_wilayah(query, parsed, implicit)
 
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
                        return await self._handle_multi_wilayah(query, parsed, mk_extra2)
 
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
        wilayah_level = "kabupaten"
        if not parsed.get("wilayah"):
            result = _fallback_detect_wilayah(query)
            logger.debug(
                "[LIST_DEBUG] fallback result=%s parsed_before_inject=%s intent_jenjang=%s",
                result, parsed, getattr(intent, 'jenjang', None)
            )
            if result:
                wilayah_name, wilayah_level = result
                parsed["wilayah"] = wilayah_name
                parsed = _inject_intent_filters(parsed, intent)
        else:
            if re.search(r"\b(desa|kelurahan)\b", q_low):
                wilayah_level = "desa_kelurahan"
            elif re.search(r"\bprovinsi\b", q_low):
                wilayah_level = "provinsi"
            elif re.search(r"\b(kecamatan|kec)\b", q_low):
                wilayah_level = "kecamatan"
            else:
                wilayah_level = "kabupaten"
 
        if not parsed.get("wilayah"):
            kw         = _extract_location_keyword(query)
            candidates = _find_location_candidates(kw) if kw else []
 
            if not candidates and kw and len(kw) >= 4:
                kw_main = _extract_main_keyword(query)
                # Guard: jika sisa keyword hanya kata level (kecamatan, kabupaten, dll)
                # jangan fuzzy — user tidak menyebut nama wilayah sama sekali
                _level_only_words = {"kecamatan", "kabupaten", "provinsi", "desa", "kelurahan", "kota"}
                kw_main_tokens = set((kw_main or "").lower().split())
                if kw_main_tokens and kw_main_tokens.issubset(_level_only_words):
                    kw_main = ""  # batalkan fuzzy
                fuzzy   = _apply_typo_correction(kw_main or kw, q_low) if kw_main else None
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
 
    async def _query_single_wilayah(
        self, query: str, parsed: dict, wilayah_level: str,
        has_level_hint: bool, intent,
        original_kw: str = "", corrected_kw: str = "",
        is_from_multikata: bool = False,
    ) -> dict:
 
        wilayah_kw = parsed.get("wilayah", "")
 
        # Provinsi dengan sibling
        if not has_level_hint and not is_from_multikata and wilayah_level == "provinsi" and wilayah_kw:
            if _has_province_siblings(wilayah_kw):
                candidates = _find_location_candidates(wilayah_kw)
                # Cek dominant prov sebelum clarification
                # Jika hanya 1 provinsi dan sisa hanya desa → auto-resolve
                prov_cands = [c for c in candidates if c["level"] == "provinsi"]
                non_prov   = [c for c in candidates if c["level"] != "provinsi"]
                if (len(prov_cands) == 1
                        and all(c["level"] == "desa_kelurahan" for c in non_prov)):
                    parsed["wilayah"] = prov_cands[0]["nama"]
                    wilayah_level     = "provinsi"
                    logger.info("[LIST] Siblings override: dominant prov '%s'", prov_cands[0]["nama"])
                else:
                    logger.info("[LIST] Province siblings found for '%s', clarification", wilayah_kw)
                    return {
                        "status": "clarification", "intent": "school_list",
                        "total": 0, "schools": [], "parsed": parsed,
                        "wilayah_level": wilayah_level, "is_ambiguous": True,
                        "keyword": wilayah_kw, "candidates": candidates,
                    }
 
        # Multi-level match
        if not has_level_hint and not is_from_multikata and wilayah_kw and _has_multi_level_match(wilayah_kw):
            candidates = _find_location_candidates(wilayah_kw)
 
            if len(candidates) == 1:
                single = candidates[0]
                parsed["wilayah"] = single["nama"]
                wilayah_level     = single["level"]
                logger.info("[LIST] Single exact candidate '%s' level=%s, skip clarification",
                            single["nama"], single["level"])
            else:
                # ── DOMINANT KAB AUTO-RESOLVE ────────────────────────
                # Jika hanya ada 1 kandidat kabupaten dan semua kandidat
                # lain (kec/desa) adalah child-nya → langsung resolve
                # ke kabupaten tanpa clarification.
                # Contoh: "kuningan" → kab. kuningan (1 kab + 1 kec + 7 desa)
                kab_candidates = [c for c in candidates if c["level"] == "kabupaten"]
                prov_candidates = [c for c in candidates if c["level"] == "provinsi"]
                if len(kab_candidates) == 1:
                    kab      = kab_candidates[0]
                    sub_levels = [c for c in candidates if c["level"] != "kabupaten"]
                    # Jika hanya ada 1 kandidat kabupaten, auto-resolve ke sana.
                    # Sub-levels (kec/desa) yang muncul dianggap noise atau
                    # disambiguasi yang tidak diperlukan — kabupaten lebih dominan.
                    parsed["wilayah"] = kab["nama"]
                    wilayah_level     = "kabupaten"
                    logger.info(
                        "[LIST] Dominant kab auto-resolve: '%s' (%d sub-candidates ignored)",
                        kab["nama"], len(sub_levels)
                    )
                    # Lanjutkan ke _build_query (jangan return clarification)
                elif len(prov_candidates) == 1 and all(
                    c["level"] in ("desa_kelurahan",) for c in candidates if c["level"] != "provinsi"
                ):
                    # ── DOMINANT PROV AUTO-RESOLVE ───────────────────────
                    # Jika hanya ada 1 kandidat provinsi dan sisa hanya desa
                    # → auto-resolve ke provinsi (mis. "banten" → prov. banten)
                    prov = prov_candidates[0]
                    sub_levels = [c for c in candidates if c["level"] != "provinsi"]
                    parsed["wilayah"] = prov["nama"]
                    wilayah_level     = "provinsi"
                    logger.info(
                        "[LIST] Dominant prov auto-resolve: '%s' (%d desa-only sub-candidates ignored)",
                        prov["nama"], len(sub_levels)
                    )
                    # Lanjutkan ke _build_query
                else:
                    # ── SAME-LOCATION CHECK ──────────────────────────────
                    # Jika semua candidates menunjuk ke kabupaten+kecamatan
                    # yang sama (mis. desa Cisauk + kec. Cisauk di Kab. Tangerang)
                    # → auto-resolve ke kecamatan (lebih luas = lebih useful)
                    kab_vals = set(c.get("kabupaten", "").lower() for c in candidates if c.get("kabupaten"))
                    kec_vals = set(c.get("kecamatan", "").lower() for c in candidates if c.get("kecamatan"))
                    if len(kab_vals) == 1 and len(kec_vals) == 1:
                        kec_name = list(kec_vals)[0]
                        kec_clean = re.sub(r"^kec\.\s*", "", kec_name).strip()
                        parsed["wilayah"] = kec_clean
                        wilayah_level = "kecamatan"
                        logger.info(
                            "[LIST] Same-location auto-resolve: all candidates → kec '%s'",
                            kec_clean
                        )
                        # Lanjutkan ke _build_query
                    else:
                        logger.info("[LIST] Multi-level clarification: wilayah=%s candidates=%d",
                                    wilayah_kw, len(candidates))
                        return {
                            "status": "clarification", "intent": "school_list",
                            "total": 0, "schools": [], "parsed": parsed,
                            "wilayah_level": wilayah_level, "is_ambiguous": True,
                            "keyword": wilayah_kw, "candidates": candidates,
                        }

        from app.services.core.es_query_builder import extract_filters_from_intent
        _intent_filters = extract_filters_from_intent(intent)
        if _intent_filters.get("bentuk_pendidikan") and not parsed.get("bentuk"):
            parsed["bentuk"] = _intent_filters["bentuk_pendidikan"]
        if _intent_filters.get("bentuk_pendidikan_in") and not parsed.get("bentuk_pendidikan_in"):
            parsed["bentuk_pendidikan_in"] = _intent_filters["bentuk_pendidikan_in"]
        if _intent_filters.get("akreditasi_exact") and not parsed.get("akreditasi"):
            parsed["akreditasi"] = _intent_filters["akreditasi_exact"]
        if _intent_filters.get("akreditasi_in") and not parsed.get("akreditasi_in"):
            parsed["akreditasi_in"] = _intent_filters["akreditasi_in"]
        if _intent_filters.get("status_sekolah") and not parsed.get("status"):
            parsed["status"] = _intent_filters["status_sekolah"]

        es_query = self._build_query(parsed, wilayah_level, query_raw=query, intent=intent)
        # Tambahkan aggregation breakdown
        es_query.update(LIST_AGGS_SNIPPET)
        result   = await self.es.search(index=self.index, body=es_query, request_timeout=5)
 
        hits      = result["hits"]["hits"]
        total_val = result["hits"]["total"]
        total     = total_val["value"] if isinstance(total_val, dict) else total_val
        schools   = [self._format_hit(h) for h in hits]
        # ── POST-SORT siswa_total (karena index: false di ES) ─────────────
        _sort_by    = getattr(intent, 'sort_by', None) if intent else None
        _sort_order = getattr(intent, 'sort_order', 'desc') if intent else 'desc'
        _sort_mode  = _detect_sort_mode(query)   # deteksi dari teks query

        # Tentukan field sort: intent.sort_by punya prioritas; fallback ke deteksi keyword
        _effective_sort = _sort_by or ("guru_total" if _sort_mode == "guru" else None)

        if _effective_sort in ("siswa_total", "guru_total") and schools:
            schools = sorted(
                schools,
                key=lambda s: s.get(_effective_sort, 0) or 0,
                reverse=(_sort_order == 'desc')
            )
            logger.debug("[LIST] post-sort by %s order=%s", _effective_sort, _sort_order)
        # Extract aggregation breakdown
        breakdown_jenjang, breakdown_status = extract_breakdowns(result)
        # Typo correction jika 0 hasil
        if total == 0 and wilayah_kw and not corrected_kw:
            kw_main = _extract_main_keyword(query)
            fuzzy   = _apply_typo_correction(kw_main or wilayah_kw, query.lower())
            if fuzzy and fuzzy[0] != wilayah_kw:
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
 
        # ── SCATTERED CHECK ──────────────────────────────────────
        # Untuk desa: cek SELALU (meski has_level_hint=True) karena
        # nama desa bisa sama di banyak provinsi.
        # Province check untuk semua desa (single-kata maupun multi-kata).
        is_desa_level = (wilayah_level == "desa_kelurahan" and bool(wilayah_kw))
        should_check_scattered = (not has_level_hint) or is_desa_level
 
        is_scattered = False
        if should_check_scattered:
            is_scattered = _check_ambiguous(
                schools, wilayah_level,
                min_distinct=3,
                use_province_check=is_desa_level,
            )
 
        if is_scattered:
            kw         = wilayah_kw or ""
            candidates = _find_location_candidates(kw) if kw else []
            logger.info("[LIST] Scattered: wilayah=%s candidates=%d", kw, len(candidates))
            return {
                "status": "clarification", "intent": "school_list",
                "total": total, "schools": [], "parsed": parsed,
                "wilayah_level": wilayah_level, "is_ambiguous": True,
                "keyword": kw, "candidates": candidates,
            }
 
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
 
    async def _handle_multi_wilayah(self, query: str, parsed: Dict, multi: Dict) -> Dict:
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
 
        bentuk    = parsed.get("bentuk")
        bentuk_in = parsed.get("bentuk_pendidikan_in")  # PAUD expand
        akreditasi = parsed.get("akreditasi")
        status    = parsed.get("status")
        
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
        if akreditasi:                                          
            filters.append({"term": {"akreditasi": akreditasi}})  
            logger.info("[LIST] Multi: filter akreditasi=%s", akreditasi)
 
        if desa:
            filters.append({"bool": {"should": [
                {"term":  {"desa_kelurahan": desa}},
                self._match_phrase("desa_kelurahan.text", desa),
            ], "minimum_should_match": 1}})
            should.append({"term": {"desa_kelurahan": {"value": desa, "boost": 30}}})
            logger.info("[LIST] Multi: filter desa=%s", desa)
 
        if kecamatan:
            filters.append({"bool": {"should": [
                {"term":  {"kecamatan": kecamatan}},
                {"term":  {"kecamatan": f"kec. {kecamatan}"}},
                self._match_phrase("kecamatan.text", kecamatan),
            ], "minimum_should_match": 1}})
            should.append({"term": {"kecamatan": {"value": kecamatan, "boost": 25}}})
            should.append({"term": {"kecamatan": {"value": f"kec. {kecamatan}", "boost": 25}}})
            logger.info("[LIST] Multi: filter kecamatan=%s", kecamatan)
 
        if kab_name:
            if want_kota:
                filters.append({"bool": {"should": [
                    {"term": {"kabupaten": f"kota {kab_name}"}},
                    {"term": {"kabupaten": f"kab. kota {kab_name}"}},
                    self._match_phrase("kabupaten.text", kab_name),
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
                    self._match_phrase("kabupaten.text", kab_name),
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

        query_body = {
            "size": 10, "track_total_hits": True,
            "query": {"bool": {"filter": filters, "should": should,
                               **({"minimum_should_match": 1} if should else {})}},
            "collapse": {"field": "npsn",
                         "inner_hits": {"name": "latest_year", "size": 1, "sort": [{"tahun": "desc"}]}},
            "sort": _build_sort_clause(_detect_sort_mode(query)),
        }
 
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
                                self._match_phrase("desa_kelurahan.text", combined),
                            ], "minimum_should_match": 1}
                        }],
                        "should": [
                            {"term": {"desa_kelurahan": {"value": combined, "boost": 30}}},
                            self._match_phrase("desa_kelurahan.text", combined),
                        ],
                        "minimum_should_match": 1,
                    }},
                    "collapse": {"field": "npsn",
                                 "inner_hits": {"name": "latest_year", "size": 1, "sort": [{"tahun": "desc"}]}},
                    "sort": _build_sort_clause(_detect_sort_mode(query)),
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
                            self._match_phrase("desa_kelurahan.text", desa),
                        ], "minimum_should_match": 1}
                    }],
                    "should": [
                        {"term": {"desa_kelurahan": {"value": desa, "boost": 30}}},
                    ],
                    "minimum_should_match": 1,
                }},
                "collapse": {"field": "npsn",
                             "inner_hits": {"name": "latest_year", "size": 1, "sort": [{"tahun": "desc"}]}},
                "sort": _build_sort_clause(_detect_sort_mode(query)),
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
 
        return {
            "status": "success", "intent": "school_list",
            "total": total, "schools": schools, "parsed": parsed_display,
            "wilayah_level": "desa_kelurahan" if desa else "kecamatan",
            "is_ambiguous": False, "candidates": [],
        }
 
    @staticmethod
    def _match_phrase(field: str, value: str) -> Dict:
        return {"match_phrase": {field: value}}
 
    @staticmethod
    def _match_phrase_boosted(field: str, value: str, boost: float) -> Dict:
        return {"bool": {"should": [{"match_phrase": {field: value}}],
                        "minimum_should_match": 1, "boost": boost}}
    
    def _build_query(self, parsed: Dict, wilayah_level: str = "kabupaten",
                    query_raw: str = "", intent=None) -> Dict:
 
        bentuk     = parsed.get("bentuk")
        bentuk_in  = parsed.get("bentuk_pendidikan_in")  # PAUD expand: list of jenjang
        wilayah    = parsed.get("wilayah")
        status     = parsed.get("status")
        akreditasi = parsed.get("akreditasi")
 
        if not status:
            sn = (parsed.get("school_name") or "").lower()
            if re.search(r"\bnegeri\b", sn) or re.search(r"\b n \b", sn):
                status = "NEGERI"
 
        # Deteksi multi-jenjang dari query: "sd, smp, sma di X" atau "sma dan sd di X"
        _JENJANG_MAP = {
            "sd": "SD", "smp": "SMP", "sma": "SMA", "smk": "SMK",
            "ma": "MA", "mts": "MTs", "mi": "MI", "tk": "TK",
            "paud": "PAUD", "kb": "KB", "ra": "RA", "sps": "SPS", "tpa": "TPA",
            "spk":    "SPK SMA",     
            "spk sd": "SPK SD",
            "spk smp": "SPK SMP",
            "spk sma": "SPK SMA",
            "spk smk": "SPK SMK",
            "spk tk":  "SPK TK",
        }
        if query_raw:
            _qr = query_raw.lower()
            _multi_jenjang = re.findall(
                r'\b(spk\s+(?:sd|smp|sma|smk|tk|kb)|sd|smp|sma|smk|ma|mts|mi|tk|paud|kb|ra|sps|tpa)\b', _qr
            )
            # Hapus duplikat, pertahankan urutan
            _seen = set(); _multi_jenjang = [x for x in _multi_jenjang if not (_seen.add(x) or x in _seen)]
            if len(_multi_jenjang) >= 2:
                # Multi-jenjang: override bentuk dengan terms filter
                _jenjang_es = []
                for j in _multi_jenjang:
                    if j == "paud":
                        _jenjang_es.extend(_PAUD_BENTUK_EXPAND)
                    else:
                        _jenjang_es.append(_JENJANG_MAP.get(j, j.upper()))
                bentuk_in = list(dict.fromkeys(_jenjang_es))  # deduplicate
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
        akreditasi_in  = parsed.get("akreditasi_in")
        akreditasi_gte = parsed.get("akreditasi_gte")
        akreditasi_exc = parsed.get("akreditasi_exclude")

        if akreditasi_in:
            filters.append({"terms": {"akreditasi": akreditasi_in}})
            logger.info("[LIST] akreditasi_in filter: %s", akreditasi_in)

        elif akreditasi_gte:
            filters.append({"terms": {"akreditasi": akreditasi_gte}})
            logger.info("[LIST] akreditasi_gte filter: %s", akreditasi_gte)

        elif akreditasi_exc:
            filters.append({
                "bool": {
                    "must_not": [
                        {"term": {"akreditasi": akreditasi_exc}}
                    ]
                }
            })
            logger.info("[LIST] akreditasi_exclude filter: %s", akreditasi_exc)

        elif akreditasi:
            filters.append({"term": {"akreditasi": akreditasi}})
            logger.info("[LIST] akreditasi filter: %s", akreditasi)
            
        # ── Filter siswa_total (range query) ────────────────────────────
        _siswa_min = intent.get('siswa_min') if isinstance(intent, dict) else getattr(intent, 'siswa_min', None)
        _siswa_max = intent.get('siswa_max') if isinstance(intent, dict) else getattr(intent, 'siswa_max', None)

        if _siswa_min is not None or _siswa_max is not None:
            range_filter = {"range": {"siswa_total": {}}}

            if _siswa_min is not None:
                range_filter["range"]["siswa_total"]["gte"] = int(_siswa_min)

            if _siswa_max is not None:
                range_filter["range"]["siswa_total"]["lte"] = int(_siswa_max)

            filters.append(range_filter)

            logger.debug("[LIST] siswa_total range filter: %s", range_filter)
            
        should = []
 
        if wilayah:
            wc        = self._strip_prefix(wilayah.lower())
            q_check   = query_raw.lower() if query_raw else ""
            want_kota = bool(re.search(r"\bkota\b", q_check)) and not bool(re.search(r"\b(kabupaten|kab)\b", q_check))
            want_kab  = bool(re.search(r"\b(kabupaten|kab)\b", q_check)) and not bool(re.search(r"\bkota\b", q_check))
 
            logger.info("[LIST] _build_query: wc=%s want_kab=%s want_kota=%s q=%s",
                        wc, want_kab, want_kota, q_check[:60])
 
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
                    self._match_phrase("desa_kelurahan.text", wc),
                    self._match_phrase("kecamatan.text", wc),
                ]
                filters.append({"bool": {"should": [
                    {"term":  {"desa_kelurahan": wc}},
                    self._match_phrase("desa_kelurahan.text", wc),
                ], "minimum_should_match": 1}})
 
            elif wilayah_level == "kecamatan":
                # Jika user eksplisit sebut "kecamatan", filter STRICT hanya kecamatan
                # Jangan ikutkan kabupaten — akan menyebabkan hasil kabupaten-wide
                q_has_kec_hint = bool(re.search(r"\b(kecamatan|kec\.?)\b", q_check))
                if q_has_kec_hint:
                    should += [
                        {"term":  {"kecamatan": {"value": wc, "boost": 25}}},
                        {"term":  {"kecamatan": {"value": f"kec. {wc}", "boost": 25}}},
                        self._match_phrase_boosted("kecamatan.text", wc, boost=25),
                    ]
                    filters.append({"bool": {"should": [
                        {"term":  {"kecamatan": wc}},
                        {"term":  {"kecamatan": f"kec. {wc}"}},
                        self._match_phrase("kecamatan.text", wc),
                    ], "minimum_should_match": 1}})
                else:
                    # Tanpa hint eksplisit: tetap include kabupaten sebagai fallback
                    should += [
                        {"term":  {"kecamatan": {"value": wc, "boost": 25}}},
                        {"term":  {"kecamatan": {"value": f"kec. {wc}", "boost": 25}}},
                        self._match_phrase_boosted("kecamatan.text", wc, boost=25),
                        {"term":  {"kabupaten": {"value": wc, "boost": 10}}},
                        {"term":  {"kabupaten": {"value": f"kab. {wc}", "boost": 10}}},
                        {"term":  {"kabupaten": {"value": f"kota {wc}", "boost": 10}}},
                        self._match_phrase("kabupaten.text", wc),
                        {"match": {"provinsi.text": {"query": wc, "boost": 3}}},
                    ]
                    filters.append({"bool": {"should": [
                        {"term":  {"kecamatan": wc}},
                        {"term":  {"kecamatan": f"kec. {wc}"}},
                        self._match_phrase("kecamatan.text", wc),
                        {"term":  {"kabupaten": wc}},
                        {"term":  {"kabupaten": f"kab. {wc}"}},
                        {"term":  {"kabupaten": f"kota {wc}"}},
                        self._match_phrase("kabupaten.text", wc),
                    ], "minimum_should_match": 1}})
 
            else:
                if want_kab:
                    should += [
                        {"term": {"kabupaten": {"value": f"kab. {wc}", "boost": 30}}},
                        {"term": {"kabupaten": {"value": wc, "boost": 5}}},
                        self._match_phrase("kecamatan.text", wc),
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
                        self._match_phrase("kecamatan.text", wc),
                        {"match": {"provinsi.text": {"query": wc, "boost": 2}}},
                    ]
                    filters.append({"bool": {"should": [
                        {"term": {"kabupaten": f"kota {wc}"}},
                        {"term": {"kabupaten": f"kab. kota {wc}"}},
                        self._match_phrase("kabupaten.text", wc),
                        self._match_phrase("kecamatan.text", wc),
                    ], "minimum_should_match": 1}})
 
                else:
                    should += [
                        {"term": {"kabupaten": {"value": wc, "boost": 20}}},
                        {"term": {"kabupaten": {"value": f"kab. {wc}", "boost": 20}}},
                        {"term": {"kabupaten": {"value": f"kota {wc}", "boost": 20}}},
                        {"term": {"kabupaten": {"value": f"kab. kota {wc}", "boost": 20}}},
                        self._match_phrase("kabupaten.text", wc),
                        self._match_phrase("kecamatan.text", wc),
                        {"match": {"provinsi.text": {"query": wc, "boost": 3}}},
                    ]
                    filters.append({"bool": {"should": [
                        {"term": {"kabupaten": wc}},
                        {"term": {"kabupaten": f"kab. {wc}"}},
                        {"term": {"kabupaten": f"kota {wc}"}},
                        {"term": {"kabupaten": f"kab. kota {wc}"}},
                        self._match_phrase("kabupaten.text", wc),
                        self._match_phrase("kecamatan.text", wc),
                    ], "minimum_should_match": 1}})
 
        query_body = {
            "size": 10, "track_total_hits": True,
            "query": {"bool": {"filter": filters, "should": should,
                               **({"minimum_should_match": 1} if should else {})}},
            "collapse": {"field": "npsn",
                         "inner_hits": {"name": "latest_year", "size": 1, "sort": [{"tahun": "desc"}]}},
            "sort": _build_sort_clause(_detect_sort_mode(query_raw)),
        }
 
        logger.info("LIST ES QUERY: %s", query_body)
        print("LIST ES QUERY:", query_body)
        return query_body
 
    def _strip_prefix(self, text: str) -> str:
        return re.sub(
            r"^(kota\s+adm\.?\s*|kota\s+|kab\.?\s+|kabupaten\s+|"
            r"prov\.?\s+|provinsi\s+|kec\.?\s+|kecamatan\s+|"
            r"desa\s+|kelurahan\s+)",
            "", text
        ).strip()
 
    def _format_hit(self, hit: Dict) -> Dict:
        if "inner_hits" in hit:
            inner = hit["inner_hits"]["latest_year"]["hits"]["hits"]
            src   = inner[0]["_source"] if inner else hit["_source"]
        else:
            src = hit["_source"]
        return {
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
        }
