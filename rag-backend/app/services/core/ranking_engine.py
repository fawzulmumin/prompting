# core/ranking_engine.py

import re
import logging
from rapidfuzz import fuzz
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# =====================================================
# LOCAL CONSTANTS (decoupled from smart_query_parser)
# =====================================================

NEGERI_INDICATORS = {
    "negeri", "sdn", "smpn", "sman", "smkn", "mtsn", "man", "min",
}

SCHOOL_VARIANTS = ["it", "islam", "islamic", "boarding", "terpadu"]
# =====================================================
# TEXT NORMALIZER
# =====================================================
 
def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
 
 
# =====================================================
# STRIP LOCATION PREFIX
# Hapus prefix administratif sebelum dibandingkan
# Contoh: "Kota Tangerang Selatan" → "tangerang selatan"
#         "Kab. Bogor"             → "bogor"
#         "Kec. Serpong"           → "serpong"
#         "Kota Adm. Jakarta Sel." → "jakarta sel."
# =====================================================
 
def strip_location_prefix(text: str) -> str:
    text = text.lower()
    text = re.sub(
        r"^(kota\s+adm\.?\s*|kota\s+|kab\.?\s+|kabupaten\s+|"
        r"prov\.?\s+|provinsi\s+|kec\.?\s+|kecamatan\s+)",
        "", text
    )
    return text.strip()
 
 
# =====================================================
# CANONICAL SCHOOL NAME
# =====================================================
 
def canonical_school_name(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\b(sd|smp|sma|smk|ma)\b", "", text)
    text = re.sub(r"\b(islam|it|terpadu)\b", "", text)
    text = re.sub(r"\d+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
 
 
# =====================================================
# SCHOOL VARIANTS
# =====================================================
 
def variant_score(query_name: str, school_name: str) -> int:
    query = normalize_text(query_name)
    name  = normalize_text(school_name)
    score = 0
    for v in SCHOOL_VARIANTS:
        if v in query and v in name:
            score += 40
        elif v in query and v not in name:
            score -= 20
    return score
 
 
# =====================================================
# EXTRACT NUMBER
# =====================================================
 
def extract_number(text: str) -> Optional[str]:
    match = re.search(r"\b\d+\b", text)
    return match.group(0) if match else None
 
 
# =====================================================
# FUZZY NAME SCORE
# =====================================================
 
def score_school(query_name: str, source: Dict) -> float:
    school_name = normalize_text(source.get("nama", ""))
    query_name  = normalize_text(query_name)
    return max(
        fuzz.token_set_ratio(query_name, school_name),
        fuzz.partial_ratio(query_name, school_name),
        fuzz.token_sort_ratio(query_name, school_name)
    )
 
 
# =====================================================
# SCHOOL TYPE SCORE
# =====================================================
 
def school_type_score(query_type, result_type) -> int:
    if not query_type:
        return 0
    if not result_type:
        return -5
    if query_type.lower() == result_type.lower():
        return 25
    return -15
 
 
# =====================================================
# LOCATION SCORE  ← Perbaikan utama di sini
#
# Logika baru:
# 1. Strip prefix dulu dari nilai ES ("Kab. Bogor" → "bogor")
# 2. Bandingkan wilayah query dengan nilai yang sudah bersih
# 3. Token overlap hanya dihitung jika semua token query ada
#    di field target (bukan hanya satu kata)
# 4. Penalti besar jika sama sekali tidak ada kecocokan
# =====================================================
 
def location_score(wilayah: str, source: Dict) -> int:
 
    if not wilayah:
        return 0
 
    wilayah_clean  = strip_location_prefix(normalize_text(wilayah))
    wilayah_tokens = set(wilayah_clean.split())
 
    raw_kab  = normalize_text(source.get("kabupaten", ""))
    raw_kec  = normalize_text(source.get("kecamatan", ""))
    raw_desa = normalize_text(source.get("desa_kelurahan", ""))
    raw_prov = normalize_text(source.get("provinsi", ""))
 
    kab  = strip_location_prefix(raw_kab)
    kec  = strip_location_prefix(raw_kec)
    desa = strip_location_prefix(raw_desa)
    prov = strip_location_prefix(raw_prov)
 
    kab_tokens  = set(kab.split())
    kec_tokens  = set(kec.split())
    desa_tokens = set(desa.split())
    prov_tokens = set(prov.split())
 
    # ── Exact full match ──────────────────────────────
    if wilayah_clean == kab:
        return 350
    if wilayah_clean == kec:
        return 280
    if wilayah_clean == desa:
        return 220
    if wilayah_clean == prov:
        return 80
 
    # ── Substring match (wilayah ada di dalam field) ──
    if wilayah_clean in kab:
        return 300
    if wilayah_clean in kec:
        return 250
    if wilayah_clean in desa:
        return 200
    if wilayah_clean in prov:
        return 60
 
    # ── Token overlap — SEMUA token query harus ada ──
    # Contoh: wilayah="tangerang selatan", kab="halmahera selatan"
    # overlap = {"selatan"} = 1 token dari 2 → ratio = 0.5 → TIDAK qualify
    # Hanya qualify jika ratio = 1.0 (semua token wilayah ada di field)
    if len(wilayah_tokens) > 1:
 
        # Semua token wilayah ada di kab?
        if wilayah_tokens.issubset(kab_tokens):
            return 250
 
        # Semua token wilayah ada di kec?
        if wilayah_tokens.issubset(kec_tokens):
            return 200
 
        # Semua token wilayah ada di desa?
        if wilayah_tokens.issubset(desa_tokens):
            return 180
 
    else:
        # Single token — partial match boleh dengan threshold lebih longgar
        token = list(wilayah_tokens)[0]
 
        if token in kab:
            return 280
        if token in kec:
            return 230
        if token in desa:
            return 190
        if token in prov:
            return 50
 
    # ── Tidak ada kecocokan sama sekali ───────────────
    return -150
 
 
# =====================================================
# NUMBER SCORE
# =====================================================
 
def number_score(query_number: str, school_name: str) -> int:
    if not query_number:
        return 0
 
    school_name_norm = normalize_text(school_name)
    name_number = extract_number(school_name_norm)
 
    if not name_number:
        return -5
 
    # Normalisasi sebagai integer agar "2" == "02"
    try:
        q_int = int(query_number)
        n_int = int(name_number)
        numbers_match = (q_int == n_int)
    except ValueError:
        numbers_match = (query_number == name_number)
 
    if not numbers_match:
        return -25
 
    # Angka cocok — cek posisi angka di nama sekolah
    # Contoh: "SMPN 1 BANDUNG" → angka di posisi awal setelah jenis sekolah = BAIK
    #         "SMP LANGLANGBUANA 1" → angka di posisi akhir = kurang relevan untuk query bernomor
    tokens = school_name_norm.split()
    num_positions = [i for i, t in enumerate(tokens) if t == name_number or t == f"0{name_number}"]
 
    if not num_positions:
        return 60  # Fallback jika tidak ketemu posisi
 
    num_pos = num_positions[0]
    total_tokens = len(tokens)
 
    # Posisi relatif (0.0 = awal, 1.0 = akhir)
    relative_pos = num_pos / max(total_tokens - 1, 1)
 
    # Angka di posisi awal/tengah (0–60%) → sekolah bernomor resmi
    # Angka di posisi akhir (>60%) → mungkin bukan nomor utama
    if relative_pos <= 0.6:
        return 120  # Bonus penuh — angka di posisi relevan
    else:
        return 60   # Bonus setengah — angka di ekor nama
 
 
# =====================================================
# NAME MATCH PENALTY
# Jika nama sekolah result TIDAK mengandung kata kunci
# utama dari query, berikan penalti tambahan.
# Mencegah sekolah dengan lokasi cocok tapi nama jauh
# mengalahkan sekolah dengan nama cocok tapi lokasi berbeda.
# =====================================================
 
def name_relevance_penalty(query_name: str, school_name: str) -> int:
    """
    Hitung penalti jika kata kunci penting dari query
    tidak muncul di nama sekolah sama sekali.
    """
    query_norm  = normalize_text(query_name)
    school_norm = normalize_text(school_name)
 
    # Hapus stopword umum
    stopwords = {"negeri", "swasta", "sd", "smp", "sma", "smk", "ma", "it"}
    query_tokens = [
        t for t in query_norm.split()
        if t not in stopwords and not t.isdigit()
    ]
 
    if not query_tokens:
        return 0
 
    # Hitung berapa token query yang ada di nama sekolah
    matched = sum(1 for t in query_tokens if t in school_norm)
    ratio   = matched / len(query_tokens)
 
    if ratio == 0:
        return -200  # Tidak ada kata kunci yang cocok sama sekali
    if ratio < 0.3:
        return -100
    if ratio < 0.5:
        return -50
 
    return 0
 
 
 
# =====================================================
# STATUS SCORE
# Penalti jika query bermaksud sekolah NEGERI
# tapi hasil berstatus SWASTA.
# Contoh: "sman 2 tangsel" → SMA Global Islamic School
# mendapat -200 karena swasta.
# =====================================================
 
def status_score(query_original: str, source: Dict) -> int:
    query_lower   = query_original.lower()
    school_status = (source.get("status_sekolah") or "").lower()
    school_name   = (source.get("nama") or "").lower()
 
    query_is_negeri = any(ind in query_lower for ind in NEGERI_INDICATORS)
    if not query_is_negeri:
        return 0
 
    negeri_prefixes = ("sman ", "smpn ", "sdn ", "smkn ", "mtsn ", "man ", "min ",
                       "sma negeri", "smp negeri", "sd negeri")
    result_is_negeri = (
        school_status in ("negeri", "n")
        or "negeri" in school_name
        or any(school_name.startswith(p) for p in negeri_prefixes)
    )
 
    if not result_is_negeri:
        return -200
 
    return 0
 
# =====================================================
# APPLY SMART RANKING
# =====================================================
 
def apply_smart_ranking(parsed_query: Dict, hits: List[Dict]) -> List[Dict]:
 
    query_name      = parsed_query.get("school_name", "")
    query_canonical = canonical_school_name(query_name)
    query_type      = parsed_query.get("bentuk")
    query_wilayah   = parsed_query.get("wilayah")
    query_number    = parsed_query.get("number")
 
    for h in hits:
 
        source      = h["_source"]
        school_name = source.get("nama", "")
        school_canonical = canonical_school_name(school_name)
 
        score = 0
 
        # BASE FUZZY MATCH
        score += score_school(query_name, source)
 
        # VARIANT BOOST
        score += variant_score(query_name, school_name)
 
        # CANONICAL NAME MATCH
        if query_canonical and query_canonical == school_canonical:
            score += 40
 
        # SCHOOL TYPE BOOST
        result_type = source.get("bentuk_pendidikan")
        score += school_type_score(query_type, result_type)
 
        # NUMBER INTELLIGENCE
        score += number_score(query_number, school_name)
 
        # NAME RELEVANCE PENALTY
        score += name_relevance_penalty(query_name, school_name)
 
        # STATUS NEGERI/SWASTA PENALTY
        score += status_score(query_name, source)
 
        # LOCATION BOOST
        loc_sc = location_score(query_wilayah, source)
        score += loc_sc
 
        # ES SCORE — dikurangi dominasinya
        es_score = float(h.get("_score") or 0)
        score += es_score * 1.5
 
        h["_custom_score"] = score
        h["_loc_score"]    = loc_sc
 
    hits.sort(key=lambda x: x["_custom_score"], reverse=True)

    if logger.isEnabledFor(logging.DEBUG):
        for i, h in enumerate(hits[:3]):
            logger.debug(
                "[RANK %d] %s | kab=%s | custom=%.1f | loc=%.1f | es=%.2f",
                i + 1,
                h["_source"].get("nama"),
                h["_source"].get("kabupaten"),
                h.get("_custom_score", 0),
                h.get("_loc_score", 0),
                h.get("_score", 0),
            )

    return hits
 
 
# =====================================================
# DETECT NAME COLLISION
# =====================================================
 
def detect_name_collision(hits) -> bool:
 
    if len(hits) < 3:
        return False
 
    canonical_names = []
    numbers = []
 
    for h in hits[:5]:
        name      = h["_source"].get("nama", "")
        canonical = canonical_school_name(name)
        number    = extract_number(name)
        canonical_names.append(canonical)
        numbers.append(number)
 
    unique_names = set(canonical_names)
 
    if len(unique_names) == 1:
        unique_numbers = set(numbers)
        if len(unique_numbers) > 1:
            return False
        return True
 
    return False
 
 
# =====================================================
# AUTO RESOLVE
# =====================================================
 
def should_auto_resolve(parsed_query: Dict, hits: List[Dict]) -> bool:
 
    if len(hits) == 0:
        return False
 
    if len(hits) == 1:
        return True
 
    top    = hits[0]
    second = hits[1]
 
    top_score    = top["_custom_score"]
    second_score = second["_custom_score"]
 
    gap = top_score - second_score
 
    query_number  = parsed_query.get("number")
    query_wilayah = parsed_query.get("wilayah")
    query_name    = parsed_query.get("school_name", "")
 
    # Gap sangat besar → auto resolve
    # KECUALI: ada konflik kabupaten vs kota
    # Contoh: "sman 7 kabupaten tangerang" → rank 1 di Kota Tangerang (bukan Kab)
    if gap > 80:
        if query_wilayah:
            wilayah_q = query_wilayah.lower()
            top_kab   = top["_source"].get("kabupaten", "").lower()  # raw, jangan normalize (butuh cek prefix "kota")
            if "kabupaten" in wilayah_q:
                wilayah_core = wilayah_q.replace("kabupaten", "").strip()
                if wilayah_core and "kota" in top_kab and wilayah_core in top_kab:
                    if len(hits) > 1:
                        rank2_kab = hits[1]["_source"].get("kabupaten", "").lower()
                        if "kab." in rank2_kab and wilayah_core in rank2_kab:
                            return False
        return True
 
    # Ada nomor + wilayah → auto resolve HANYA jika gap cukup (>= 10)
    # Fix: kondisi lama terlalu permisif — "SMAN 1 Bogor" dengan 294 hasil
    # semua punya score hampir sama (gap=0.8) tapi langsung di-resolve ke rank 1.
    # Sekarang: nomor+wilayah hanya auto-resolve jika ada pemenang yang jelas.
    if query_number and query_wilayah and gap >= 10:
        # Cek kabupaten vs kota sebelum auto-resolve
        if query_wilayah and len(hits) >= 2:
            wq = query_wilayah.lower()
            if "kabupaten" in wq:
                core = wq.replace("kabupaten", "").strip()
                t_kab = hits[0]["_source"].get("kabupaten", "").lower()
                r_kab = hits[1]["_source"].get("kabupaten", "").lower()
                if core and "kota" in t_kab and core in t_kab and "kab." in r_kab and core in r_kab:
                    return False
        return True
 
    # FIX 3: Exact/near-exact name match pada top hit
    # Jika nama query sangat mirip dengan top hit (>90 fuzzy score),
    # dan gap cukup (>30), auto resolve
    if query_name and gap > 30:
        top_name  = normalize_text(top["_source"].get("nama", ""))
        query_norm = normalize_text(query_name)
        name_similarity = max(
            fuzz.token_set_ratio(query_norm, top_name),
            fuzz.partial_ratio(query_norm, top_name),
        )
        if name_similarity >= 88:
            return True
 
    if detect_name_collision(hits):
        return False
 
    # Hanya 1 kandidat di wilayah yang diminta
    if query_wilayah:
        wilayah_clean = strip_location_prefix(normalize_text(query_wilayah))
 
        wilayah_hits = [
            h for h in hits
            if wilayah_clean in strip_location_prefix(
                normalize_text(h["_source"].get("kabupaten", ""))
            )
            or wilayah_clean in strip_location_prefix(
                normalize_text(h["_source"].get("kecamatan", ""))
            )
        ]
 
        if len(wilayah_hits) == 1:
            return True
 
    return False
