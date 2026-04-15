# query_intent_router.py
 
import re
from enum import Enum
 
 
class QueryIntent(str, Enum):
    SCHOOL_PROFILE = "school_profile"
    SCHOOL_LIST    = "school_list"
    UNKNOWN        = "unknown"
 
 
# =====================================================
# PROFILE PATTERNS — prioritas lebih tinggi dari LIST
# Jika match salah satu ini → PROFILE, tidak diperiksa LIST
# =====================================================
PROFILE_PATTERNS = [
    # Kata eksplisit profil
    r"\b(profil|profile|info|informasi|detail|data)\b",
    # Nomor spesifik setelah jenis sekolah — PALING PENTING
    # Hanya match jika ada angka (bukan kata seperti "negeri", "akreditasi", dll)
    r"\b(sd|sdn|smp|smpn|sma|sman|smk|smkn|ma|man|mts|mtsn)\s+(negeri\s+|swasta\s+)?\d+\b",
    # NPSN 8 digit
    r"\b\d{8}\b",
    # Atribut sekolah spesifik TANPA akreditasi (akreditasi sudah di LIST)
    r"\b(npsn|alamat|jumlah siswa|jumlah guru|kurikulum)\b",
    # Pertanyaan lokasi sekolah spesifik ("dimana", "ada dimana", "letak")
    r"\b(dimana|di mana|letaknya|lokasinya|alamatnya|ada dimana)\b",
    # Nama sekolah unik dengan nomor — HANYA jika ada angka setelah nama
    # Tidak match "sma negeri akreditasi b" karena "b" bukan angka dan ada "akreditasi"
    r"\b(sd|sdn|smp|smpn|sma|sman|smk|smkn|ma|man|mts|mtsn)\s+(?!negeri\s+di\b|swasta\s+di\b|negeri\s+akreditasi\b|swasta\s+akreditasi\b|negeri\s*$|swasta\s*$|di\b)(\w+)\s+\d+\b",
]
 
# =====================================================
# LIST PATTERNS — hanya jika tidak match PROFILE
# =====================================================
LIST_PATTERNS = [
    # Eksplisit "daftar" dan alias-nya (list, lits, tampilkan, dll)
    r"\bdaftar\b",
    r"\blist\b",       # "list sekolah di X"
    r"\blits\b",       # typo dari "list"
    r"\blst\b",        # typo dari "list"
    # "ada berapa sekolah" / "berapa sekolah"
    r"\bberapa\s+(sekolah|sd|smp|sma|smk)\b",
    # "sekolah apa saja" / "sekolah mana saja"
    r"\bsekolah\b.{0,20}\b(apa|mana)\s+saja\b",
    # "sekolah negeri di X" tanpa nomor (pure list query)
    r"\bsekolah\s+(negeri|swasta)\b.{0,30}\bdi\b",
    # "smp di X" / "sma di X" — hanya tanpa nomor dan tanpa nama spesifik
    r"^(sd|smp|sma|smk|ma|mts)\s+(negeri\s+|swasta\s+)?di\b",
    r"\b(sd|smp|sma|smk|ma|mts)\s+(negeri|swasta)\s+di\b",
    # "tampilkan / cari / carikan sekolah"
    r"\b(tampilkan|carikan|cari|rekomendasikan)\b.{0,30}\bsekolah\b",
    # "terakreditasi X" / "akreditasi X" tanpa nomor spesifik → list query
    r"\b(terakreditasi|berakreditasi|akreditasi)\s+[abc]\b",
    # "sekolah ... akreditasi" → likely list
    r"\bsekolah\b.{0,40}\bakreditasi\b",
        # Semua jenjang (termasuk paud, tk, kb) diikuti 'di [lokasi]'
    r'^(sd|sdn|smp|sma|smk|ma|mts|tk|paud|kb|ra|sps|tpa)\s+(negeri\s+|swasta\s+)?di\b',
    r'\b(tk|paud|kb|ra|sps|tpa)\s+(?:negeri\s+|swasta\s+)?di\b',
    # 'sekolah di [lokasi]' tanpa keyword eksplisit
    r'\\bsekolah\\s+(?:negeri\\s+|swasta\\s+|[a-z]+\\s+)?di\\s+\\w+',
    # multi-jenjang: "sd, smp, sma di X" atau "sma dan sd di X"
    r'\\b(sd|smp|sma|smk|tk|mi|mts|ma|paud|kb)(\\s*[,/]\\s*|\\s+(dan|atau|dengan)\\s+)(sd|smp|sma|smk|tk|mi|mts|ma|paud|kb).{0,30}\\bdi\\b',
    # "bandingkan sekolah SMA dan SD di X" → sebenarnya list multi-jenjang
    r'\\b(bandingkan\\s+)?sekolah\\s+(sd|smp|sma|smk|tk|mi|mts|ma|paud|kb)(\\s*(dan|atau|,)\\s*(sd|smp|sma|smk|tk|mi|mts|ma|paud|kb))+\\s+(di|dalam)\\b',
]
 
 
def detect_query_intent(query: str) -> QueryIntent:
 
    if not query:
        return QueryIntent.UNKNOWN
 
    q = query.lower().strip()
 
    # PROFILE cek duluan — lebih spesifik
    for pattern in PROFILE_PATTERNS:
        if re.search(pattern, q):
            return QueryIntent.SCHOOL_PROFILE
 
    # LIST cek setelahnya
    for pattern in LIST_PATTERNS:
        if re.search(pattern, q):
            return QueryIntent.SCHOOL_LIST
 
    # Default ke PROFILE jika query mengandung nama sekolah apapun
    return QueryIntent.SCHOOL_PROFILE
