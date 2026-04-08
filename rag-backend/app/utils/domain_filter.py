# utils/domain_filter.py
import re

# ─────────────────────────────────────────────────────────────────────────────
# FAST-REJECT: pola yang PASTI di luar domain pendidikan
# ─────────────────────────────────────────────────────────────────────────────
_OUT_OF_DOMAIN_PATTERNS = [
    # Hewan
    r'\b(badak|gajah|harimau|singa|ular|buaya|monyet|kucing|anjing|kuda|sapi|'
    r'zebra|jerapah|serigala|rubah|beruang|koala|kanguru)\b',

    # Transportasi & wisata
    r'\b(tiket|pesawat|kereta|kapal|travel|liburan|wisata|hotel|penginapan)\b',

    # Makanan
    r'\b(makanan|restoran|resep|masak|kuliner|warung|kafe)\b',

    # Cuaca & bencana alam
    r'\b(cuaca|suhu|iklim|prakiraan|gempa|tsunami|banjir|erupsi|gunung meletus)\b',

    # Hiburan & media
    r'\b(artis|film|bioskop|musik|lagu|konser|netflix|drakor|sinetron)\b',

    # Kesehatan & medis
    r'\b(obat|penyakit|gejala|dokter|rumah sakit|virus|vaksin|imunisasi|'
    r'covid|diabetes|kanker|demam berdarah)\b',

    # Ekonomi & keuangan
    r'\b(saham|ihsg|kurs|inflasi|resesi|bitcoin|kripto|ethereum|'
    r'harga emas|harga minyak|harga bbm|suku bunga)\b',

    # Politik & pemerintahan
    r'\b(presiden|gubernur|bupati|walikota|menteri|dpr|dprd|mpr|'
    r'pilpres|pilkada|pemilu|korupsi|pengadilan|pidana|kriminal)\b',

    # Pertanian & komoditas
    r'\b(sawit|kelapa sawit|padi|jagung|kedelai|kakao|kopi|'
    r'pestisida|pupuk|panen|ternak|peternakan)\b',

    # Teknologi non-pendidikan
    r'\b(chatgpt|openai|gemini|iphone|samsung|android)\b',

    # Negara asing
    r'\b(israel|palestina|rusia|ukraina|korea utara|korea selatan|'
    r'inggris|prancis|jerman|australia|kanada|brazil|arab saudi|turki|iran)\b',
]

_OUT_OF_DOMAIN_RE = [re.compile(p, re.IGNORECASE) for p in _OUT_OF_DOMAIN_PATTERNS]

# ─────────────────────────────────────────────────────────────────────────────
# DEFINITIONAL PATTERN
# ─────────────────────────────────────────────────────────────────────────────
_DEFINITIONAL_RE = re.compile(
    r'(?:'
    r'\b\w+(?:\s+\w+){0,5}\s+itu\s+apa\b'
    r'|\bapa\s+itu\s+\w+(?:\s+\w+){0,3}\b'
    r'|\b\w+(?:\s+\w+){0,5}\s+adalah\s+apa\b'
    r'|\b\w+(?:\s+\w+){0,4}\s+dimana\b'
    r'|\bdimana\s+\w+(?:\s+\w+){0,4}\b'
    r'|\bsiapa\s+(?:itu\s+)?\w+(?:\s+\w+){0,3}\b'
    r'|\bapa\s+yang\s+dimaksud\b'
    r')',
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
# EDUCATION KEYWORDS
#
# PENTING: pisah dua kelompok berdasarkan panjang kata:
#
# _EDUCATION_KEYWORDS_LONG  → kata >= 4 karakter, aman pakai substring `in`
# _EDUCATION_KEYWORDS_SHORT → kata <= 3 karakter, WAJIB word boundary regex
#                             karena berisiko match substring
#                             Contoh: "man" match "polandia", "sma" match "plasma"
# ─────────────────────────────────────────────────────────────────────────────

# Kata panjang (>= 4 karakter) — aman pakai `in`
_EDUCATION_KEYWORDS_LONG = [
    # Kata inti domain
    "sekolah", "persekolahan",
    # Singkatan jenjang dengan "n" (>= 4 karakter)
    "sman", "smpn", "smkn", "mtsn",
    # Singkatan umum >= 4 karakter
    "paud", "slb",
    # Nama lengkap jenjang
    "madrasah", "ibtidaiyah", "tsanawiyah", "aliyah",
    "pesantren", "pondok pesantren", "taman kanak",
    # Identitas sekolah
    "npsn", "dapodik", "akreditasi",
    # Pelaku pendidikan
    "siswa", "murid", "peserta didik",
    "guru", "tenaga pendidik", "kepala sekolah",
    "honorer", "sertifikasi guru",
    # Akademik
    "kurikulum", "rapor",
]

# Kata pendek (<= 3 karakter) — HARUS word boundary, jangan pakai `in`
# Dikompilasi jadi satu regex OR untuk efisiensi
_EDUCATION_KEYWORDS_SHORT_RE = re.compile(
    r'\b(sd|mi|ma|tk|kb|smk|sma|smp|mts|sdn|tkn|min|man)\b',
    re.IGNORECASE,
)

# Pattern frasa multi-kata spesifik sekolah
_EDUCATION_PATTERNS = [
    r'\b(daftar|jumlah|profil|data)\s+sekolah\b',
    r'\bsekolah\s+(di|negeri|swasta|dasar|menengah|atas|kejuruan)\b',
    r'\bbandingkan\b.{1,60}\bsekolah\b',
    r'\bsekolah\b.{1,40}\b(vs|versus|bandingkan)\b',
]

_EDUCATION_RE = [re.compile(p, re.IGNORECASE) for p in _EDUCATION_PATTERNS]

# Kata konteks sekolah untuk validasi di step definitional
# HANYA kata >= 4 karakter untuk menghindari substring false positive
_SCHOOL_CONTEXT_WORDS = {
    "sekolah", "siswa", "guru", "npsn", "akreditasi", "madrasah",
    "pesantren", "murid", "kurikulum", "smkn", "sman", "smpn",
    "mtsn", "dapodik", "honorer",
}


def is_education_query(query: str) -> bool:
    """
    Guard layer kedua setelah intent_classifier.
    Return False → query pasti di luar domain → reject.
    Return True  → query kemungkinan terkait pendidikan → lanjut routing ES.
    """
    q = query.lower().strip()

    if not q:
        return False

    # ── 1. Fast-reject: pola OOC yang pasti salah domain ────────────────────
    for pattern in _OUT_OF_DOMAIN_RE:
        if pattern.search(q):
            return False

    # ── 2a. Education keyword panjang (substring aman) ───────────────────────
    for kw in _EDUCATION_KEYWORDS_LONG:
        if kw in q:
            return True

    # ── 2b. Education keyword pendek (word boundary wajib) ───────────────────
    if _EDUCATION_KEYWORDS_SHORT_RE.search(q):
        return True

    # ── 3. Education pattern (frasa multi-kata) ──────────────────────────────
    for pattern in _EDUCATION_RE:
        if pattern.search(q):
            return True

    # ── 4. Definitional pattern tanpa konteks sekolah → reject ──────────────
    if _DEFINITIONAL_RE.search(q):
        # Cek konteks sekolah: hanya kata panjang yang aman
        has_school_context = any(word in q for word in _SCHOOL_CONTEXT_WORDS)
        if not has_school_context:
            return False

    # ── 5. Tidak ada sinyal pendidikan yang jelas → tolak ───────────────────
    return False