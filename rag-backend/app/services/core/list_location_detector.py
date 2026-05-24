# list_location_detector.py
"""
Location detection helpers diekstrak dari list_school_handler.py (Phase 13).
Berisi 10 pure/utility functions untuk deteksi dan resolusi wilayah pada list queries.
"""

import re
import logging
from typing import Dict, List, Optional, Tuple, Set
from app.services.core.location_resolver import strip_loc_prefix

logger = logging.getLogger(__name__)

# ================================================================
# MODULE-LEVEL CONSTANTS (dipindah dari list_school_handler)
# ================================================================

# Token kondisi numerik yang harus di-strip sebelum ekstraksi keyword lokasi.
# HANYA dipakai di _extract_location_keyword — bukan untuk tujuan lain.
_WILAYAH_QUALIFIER_PATTERNS = re.compile(
    r'\b('
    r'wilayah\s+(?:terpencil|perbatasan|3[Tt]|terdepan|tertinggal)|'
    r'daerah\s+(?:terpencil|perbatasan|3[Tt]|terdepan|tertinggal)|'
    r'terpencil|perbatasan|tertinggal|terdepan|'
    r'3[Tt]\s+(?:terpencil|terdepan|tertinggal)'
    r')\b',
    re.IGNORECASE
)

_NUMERIC_CONDITION_PATTERNS = re.compile(
    r'\b('
    # Compound: subject noun + numeric operator — strip both together so "guru kurang dari" vanishes
    r'(?:(?:jumlah\s+)?(?:guru|siswa|murid|kelas|komputer|toilet|ruang|tendik)\s+'
    r'(?:lebih dari|kurang dari|di atas|di bawah|minimal|maksimal|'
    r'setidaknya|paling banyak|paling sedikit|antara|sampai|hingga))|'
    # Compound: "dengan [noun]" — strip the whole prep phrase so "dengan guru" vanishes
    r'(?:dengan\s+(?:jumlah\s+)?(?:guru|siswa|murid|kelas|komputer|toilet|ruang|tendik))|'
    # Individual operators
    r'lebih dari|kurang dari|di atas|di bawah|'
    r'minimal|maksimal|minimum|maximum|'
    r'setidaknya|paling banyak|paling sedikit|'
    r'yang punya|yang memiliki|yang ada|'
    r'dengan|punya|memiliki|'
    r'antara|sampai|hingga|'
    r'\d+\s*(?:siswa|guru|murid|kelas|komputer|toilet|ruang|orang|unit)?'
    r')\b',
    re.IGNORECASE
)


def _strip_numeric_condition_tokens(query: str) -> str:
    """
    Hapus token kondisi numerik dan qualifier wilayah dari query sebelum ekstraksi keyword lokasi.
    Contoh: "SMA dengan guru lebih dari 5 di Sulawesi" → "SMA di Sulawesi"
    Contoh: "daftar SMP di wilayah perbatasan Kalimantan" → "daftar SMP di Kalimantan"
    HANYA dipanggil untuk keyword lokasi — query asli tetap diteruskan ke pipeline lain.
    """
    cleaned = _WILAYAH_QUALIFIER_PATTERNS.sub(' ', query)
    cleaned = _NUMERIC_CONDITION_PATTERNS.sub(' ', cleaned)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    if cleaned != query:
        logger.info("[LIST] stripped tokens: '%s' → '%s'", query[:60], cleaned[:60])
    return cleaned


def _resolve_pulau_keyword(kw: str):
    """
    Cek apakah keyword adalah nama pulau/region besar (Jawa, Sulawesi, dll).
    Menerima "pulau jawa" atau "jawa" langsung.
    Return (pulau_name, provinsi_list) atau None.
    """
    from app.services.core.aggregation_core import PULAU_PROVINSI_MAP
    kw_clean = re.sub(r'^pulau\s+', '', kw.lower().strip())
    if kw_clean in PULAU_PROVINSI_MAP:
        return kw_clean, PULAU_PROVINSI_MAP[kw_clean]
    return None


_AMBIGUOUS_SINGLE_WORDS = {
    "timur", "barat", "utara", "selatan", "tengah",
    "dalam", "luar", "baru", "lama", "raya", "induk",
}

_NON_STANDALONE_LOCATION_WORDS = {
    "barat", "timur", "utara", "selatan", "tengah",
    "dalam", "luar", "baru", "lama", "raya",
}

# Kata-kata yang menandakan nama wilayah sudah selesai
_FILTER_BOUNDARY_WORDS = {
    "yang", "akreditasi", "jumlah", "siswa", "murid", "urutkan",
    "dengan", "diatas", "di atas", "dibawah", "di bawah", "atas",
    "negeri", "swasta", "terbanyak", "terbesar", "terkecil",
    "paling", "minimal", "maksimal", "lebih", "kurang",
    "dan", "atau", "serta",
}


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
    Deteksi pola dua atau lebih lokasi dipisah "dan"/"serta" (atau koma+dan).
    Contoh 2 lokasi: "cibunar parungpanjang dan lumpang parungpanjang"
    Contoh 3 lokasi: "sekolah di bandung, bogor, dan bekasi"
    """
    # GUARD: jangan split jika "dan" ada di antara dua token akreditasi/jenjang
    q_low = query.lower()
    if re.search(r'terakreditasi\s+\w+\s+dan\s+terakreditasi', q_low):
        return None

    # Guard: jangan split jika ada kata filter di query
    _FILTER_WORDS = {"akreditasi", "negeri", "swasta", "status", "jumlah",
                     "terbanyak", "urutkan", "minimal", "maksimal"}
    if any(fw in q_low.split() for fw in _FILTER_WORDS):
        return None

    sw = {"di", "daftar", "sekolah", "paud", "sd", "smp", "sma",
          "smk", "tk", "ra", "kb", "dan", "serta", "atau"}

    def has_loc_token(chunk: str) -> bool:
        return any(len(t) >= 3 and t not in sw for t in chunk.split())

    # Ambil prefix (bagian sebelum nama lokasi, mis. "sekolah di ")
    prefix_match = re.match(r"^(.*?\bdi\b\s*)", q_low)
    prefix = prefix_match.group(1) if prefix_match else ""

    def _clean_chunk(chunk: str) -> str:
        p = prefix.strip()
        c = chunk.strip()
        if p and c.startswith(p):
            c = c[len(p):].strip()
        return c

    # ── PATH A: Koma + "dan/serta" (3+ lokasi) ──────────────────────────
    # Contoh: "bandung, bogor, dan bekasi" → ["bandung", "bogor", "bekasi"]
    if "," in query and re.search(r",\s*(?:dan|serta)\s+", q_low):
        # Ganti ", dan " / ", serta " dengan koma biasa, lalu split pada koma
        normalized = re.sub(r",\s*(?:dan|serta)\s+", ",", q_low)
        # Juga split pada koma yang tersisa
        raw_parts = [p.strip() for p in normalized.split(",") if p.strip()]
        if len(raw_parts) >= 2:
            locs = [_clean_chunk(p) for p in raw_parts]
            locs = [l for l in locs if has_loc_token(l)]
            if len(locs) >= 2:
                logger.debug("[DAN_SPLIT] comma+dan path: %d locs", len(locs))
                return [prefix + loc for loc in locs]

    # ── PATH B: Dua bagian dipisah "dan/serta/atau" (pola lama) ─────────
    q_clean = re.sub(r"[^\w\s]", " ", q_low).strip()
    _SPLITTERS = r"\b(dan|serta|atau|dengan|beserta|maupun|juga|ataupun|sama)\b"
    parts = re.split(_SPLITTERS, q_clean)
    chunks = [p.strip() for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]
    if len(chunks) != 2:
        return None
    if not (has_loc_token(chunks[0]) and has_loc_token(chunks[1])):
        return None
    return [prefix + _clean_chunk(chunks[0]), prefix + _clean_chunk(chunks[1])]


# ================================================================
# MULTI-WILAYAH EXTRACTION (explicit dengan hint level)
# ================================================================

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
            return len(strip_loc_prefix(key))

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
    clean_query = _strip_numeric_condition_tokens(query)
    if clean_query != query:
        logger.info("[LIST] stripped numeric tokens: '%s' → '%s'", query[:60], clean_query[:60])
    tokens = re.sub(r"[^\w\s]", " ", clean_query.lower()).split()
    keywords = [t for t in tokens if t not in stopwords and len(t) >= 3]
    return " ".join(keywords).strip()
