# smart_query_parser.py
 
import re
from typing import Dict, Optional, Any
 
# =====================================================
# CONSTANTS
# =====================================================
 
SCHOOL_TYPES = [
    "SD", "TK", "KB", "SMP", "RA", "MI", "SPS", "MTs", "SMA", "SMK",
    "Kursus", "PKBM", "MA", "Universitas", "Sekolah Tinggi",
    "PAUDQ", "TPA", "SLB", "Pondok Pesantren", "Akademik",
    "Institut", "SKB", "Politeknik", "SPK SD", "SPK SMP",
    "SMTK", "SMPTK", "SPK SMA", "SPM Ulya", "SPM Wustha",
    "Pratama Widyalaya", "SPK TK", "SDTK", "Taman Seminari",
    "SMAK", "Akademi", "SPK KB", "SMAg.K", "Akademi Komunitas",
    "Nava Dhammasekha", "PDF Ulya", "PDF Wustha", "Adi Widyalaya",
    "Madyama Widyalaya", "MAK", "SPM Ula", "Mula Dhammasekha",
    "Utama Widyalaya", "Pratama W P", "Utama Widyalaya Kejuruan",
    "Uttama Dhammasekha",
]
 
ABBREVIATIONS = {
    "sdn":   "sd negeri",
    "sd n":  "sd negeri",
    "smpn":  "smp negeri",
    "smp n": "smp negeri",
    "sman":  "sma negeri",
    "sma n": "sma negeri",
    "smkn":  "smk negeri",
    "smk n": "smk negeri",
    "mtsn":  "mts negeri",
    "min":   "mi negeri",
    "negri": "negeri",
    # MAN tidak di-expand: nama di ES tetap "MAN ...", bukan "MA NEGERI ..."
    "ponpes": "pondok pesantren",
    "univ":   "universitas",
    "poltek": "politeknik",
}
 
# =====================================================
# Mapping kata kunci mentah → (bentuk_es, status)
# Dijalankan SEBELUM normalisasi agar "man", "mtsn",
# "min" tidak hilang saat di-expand oleh ABBREVIATIONS
# =====================================================
_RAW_BENTUK_STATUS = [
    # (pattern, bentuk_di_ES, status)
    # Madrasah Negeri — cek lebih dulu sebelum yang umum
    (r"\bman\b",          "MA",   "NEGERI"),
    (r"\bmtsn\b",         "MTs",  "NEGERI"),
    (r"\bmin\b",          "MI",   "NEGERI"),
    (r"\bsdn\b",          "SD",   "NEGERI"),
    (r"\bsd\s*n\b",       "SD",   "NEGERI"),
    (r"\bsmpn\b",         "SMP",  "NEGERI"),
    (r"\bsmp\s*n\b",      "SMP",  "NEGERI"),
    (r"\bsman\b",         "SMA",  "NEGERI"),
    (r"\bsma\s*n\b",      "SMA",  "NEGERI"),
    (r"\bsmkn\b",         "SMK",  "NEGERI"),
    (r"\bsmk\s*n\b",      "SMK",  "NEGERI"),
    # Swasta — biasanya tidak ada suffix tapi bisa cek eksplisit
    (r"\bsds\b",          "SD",   "SWASTA"),
    (r"\bsmps\b",         "SMP",  "SWASTA"),
    (r"\bsmas\b",         "SMA",  "SWASTA"),
    (r"\bsmks\b",         "SMK",  "SWASTA"),
    # Bentuk saja (tanpa status)
    (r"\bma\b",           "MA",   None),
    (r"\bmts\b",          "MTs",  None),
    (r"\bmi\b",           "MI",   None),
    (r"\bmak\b",          "MAK",  None),
    (r"\bsd\b",           "SD",   None),
    (r"\bsmp\b",          "SMP",  None),
    (r"\bsma\b",          "SMA",  None),
    (r"\bsmk\b",          "SMK",  None),
    (r"\btk\b",           "TK",   None),
    (r"\bra\b",           "RA",   None),
    (r"\bkb\b",           "KB",   None),
    (r"\btpa\b",          "TPA",  None),
    (r"\bsps\b",          "SPS",  None),
    (r"\bpaud\b",         "PAUD", None),
    (r"\bpaudq\b",        "PAUDQ",None),
    (r"\bslb\b",          "SLB",  None),
    (r"\bpkbm\b",         "PKBM", None),
    (r"\bskb\b",          "SKB",  None),
]
 
# Token yang menandakan query bermaksud sekolah NEGERI
NEGERI_INDICATORS = {
    "negeri", "sdn", "smpn", "sman", "smkn", "mtsn", "man", "min",
}
 
STOPWORDS = [
    # Kata baku
    "profil",
    "sekolah",
    "data",
    "tentang",
    "informasi",
    "dengan",
    "nama",
    "bernama",
    "yang",
    "ada",
    "di",
    # Kata tanya & pertanyaan — TAMBAHAN
    "apa",
    "apa itu",
    "apakah",
    "dimana",
    "di mana",
    "bagaimana",
    "berapa",
    "siapa",
    "kapan",
    "kenapa",
    "mengapa",
    "gimana",
    "saja",
    "apa saja",
    "dong",
    "dong",
    "yuk",
    "tolong",
    "mohon",
    "coba",
    "dong",
    # Kata tanya dalam konteks sekolah
    "akreditasinya",
    "akreditasi",
    # FIX Bug 1
    "terakreditasi",
    "berakreditasi",
    "berstatus",
    "berstatuskan",
    "lokasinya",
    "alamatnya",
    "statusnya",
    "jenjangnya",
    "profilnya",
    "letaknya",
    "terletak",
    "berada",
    "ada",
    "terdapat",
    # Typo variants
    "prfil",
    "profl",
    "profli",
    "seklah",
    "seklh",
    "sekolh",
    "sekola",
    "sekolhnya",
    "nam",
    "nma",
    "brnama",
    "bernma",
]
 
WILAYAH_ALIAS = {
    "tangsel": "tangerang selatan",
    "jaksel":  "jakarta selatan",
    "jakbar":  "jakarta barat",
    "jakpus":  "jakarta pusat",
    "jakut":   "jakarta utara",
    "jakti":   "jakarta timur",
}
 
COMMON_CITIES = [
    # Multi-kata (lebih spesifik)
    "tangerang selatan",
    "jakarta selatan",
    "jakarta barat",
    "jakarta pusat",
    "jakarta utara",
    "jakarta timur",
    "bandung barat",
    "bekasi barat",
    "bekasi utara",
    "bekasi selatan",
    "bekasi timur",
    "bogor barat",
    "bogor utara",
    "bogor selatan",
    "bogor timur",
    "bogor tengah",
    # Kata tunggal
    "jakarta",
    "bandung",
    "surabaya",
    "tangerang",
    "bekasi",
    "depok",
    "bogor",
    "semarang",
    "yogyakarta",
    "medan",
    "makassar",
    "palembang",
    "pekanbaru",
    "balikpapan",
    "banjarmasin",
    "malang",
    "solo",
    "surakarta",
    "denpasar",
    "mataram",
    "kupang",
    "pontianak",
    "samarinda",
    "manado",
    "ambon",
    "jayapura",
]
 
KNOWN_DISTRICTS = [
    # Tangerang Selatan
    "pondok aren",
    "ciputat timur",
    "serpong utara",
    "serpong",
    "ciputat",
    "pamulang",
    "setu",
    # Tangerang Kab
    "pagedangan",
    "cisauk",
    "legok",
    "kelapa dua",
    "curug",
    "cikupa",
    "tigaraksa",
    "balaraja",
    "kronjo",
    "pakuhaji",
    # Bogor Kab
    "parungpanjang",
    "gunung sindur",
    "rumpin",
    "cigudeg",
    "tenjo",
    "jasinga",
    "leuwiliang",
    "cibungbulang",
    "dramaga",
    "ciawi",
    "caringin",
    "cigombong",
    "ciomas",
    "kemang",
    "parung",
    "ciseeng",
    "tajurhalang",
    "cibinong",
    "cileungsi",
    "jonggol",
    "cariu",
    "tanjungsari",
    "sukamakmur",
    "klapanunggal",
    "gunungputri",
    "citeureup",
    "babakan madang",
    # Depok
    "sukmajaya",
    "cimanggis",
    "beji",
    "sawangan",
    "limo",
    "pancoran mas",
    "tapos",
    # Bekasi
    "tambun",
    "cikarang",
    "cibitung",
    "tarumajaya",
    "babelan",
    "muaragembong",
    # Jakarta
    "pasar minggu",
    "kebayoran lama",
    "kebayoran baru",
    "pesanggrahan",
    "cilandak",
    "jagakarsa",
    "mampang",
    "tebet",
    "setiabudi",
    "menteng",
    "gambir",
    "sawah besar",
    "kemayoran",
    "senen",
    "cempaka putih",
    "johar baru",
    "tanah abang",
    "penjaringan",
    "pademangan",
    "tanjung priok",
    "koja",
    "cilincing",
    "kelapa gading",
    "matraman",
    "jatinegara",
    "kramat jati",
    "pulogadung",
    "cakung",
    "duren sawit",
    "cipayung",
    "pasar rebo",
    "ciracas",
]
 
SCHOOL_VARIANTS = ["it", "islam", "islamic", "boarding", "terpadu"]
 
DIRECTION_WORDS = ["selatan", "barat", "timur", "utara", "pusat"]
 
 
# =====================================================
# DETECT BENTUK + STATUS (dari query MENTAH, sebelum normalisasi)
# Return (bentuk, status) — keduanya bisa None
# =====================================================
 
def detect_bentuk_and_status(raw_query: str) -> tuple[Optional[str], Optional[str]]:
    q = raw_query.lower()

    # FIX: Tangkap "berstatus negeri/swasta" eksplisit sebelum fallback
    _STATUS_MAP = {"negeri": "Negeri", "swasta": "Swasta"}
    _m = re.search(
        r'\b(?:berstatus|berstatuskan|status(?:nya)?)?\s*(negeri|swasta)\b', q
    )
    if _m:
        status_val = _STATUS_MAP[_m.group(1)]
        for pattern, bentuk, _ in _RAW_BENTUK_STATUS:
            if re.search(pattern, q):
                return bentuk, status_val
        return None, status_val

    # =====================================================
    # EXISTING LOGIC (JANGAN DIHAPUS)
    # =====================================================
    for pattern, bentuk, status in _RAW_BENTUK_STATUS:
        if re.search(pattern, q):
            return bentuk, status

    return None, None
 
 
# =====================================================
# NORMALIZE QUERY
# =====================================================
 
def normalize_query(query: str) -> str:
    query = query.lower()
 
    for short, full in ABBREVIATIONS.items():
        query = re.sub(rf"\b{short}\b", full, query)
 
    query = re.sub(r"[^\w\s]", " ", query)
 
    for word in STOPWORDS:
        query = re.sub(rf"\b{word}\b", "", query)
 
    query = re.sub(r"\s+", " ", query)
 
    return query.strip()
 
 
# =====================================================
# EXTRACT YEAR
# =====================================================
 
def extract_year(query: str) -> Optional[int]:
    match = re.search(r"\b(20\d{2})\b", query)
    return int(match.group(1)) if match else None
 
 
# =====================================================
# EXTRACT SCHOOL NUMBER
# =====================================================
 
def extract_school_number(query: str) -> Optional[str]:
    match = re.search(r"\b\d{1,3}\b", query)
    return match.group(0) if match else None
 
 
# =====================================================
# DETECT SCHOOL TYPE (dari normalized query)
# =====================================================
 
def detect_school_type(query: str) -> Optional[str]:
    for school_type in SCHOOL_TYPES:
        pattern = rf"\b{school_type.lower()}\b"
        if re.search(pattern, query):
            return school_type
    return None
 
 
# =====================================================
# EXTRACT WILAYAH
# =====================================================
 
def extract_wilayah(query: str) -> Optional[str]:
 
    # 1. Alias
    for alias, full in WILAYAH_ALIAS.items():
        if re.search(rf"\b{alias}\b", query):
            return full
 
    # 2. Pola eksplisit "kota X" / "kabupaten X"
    match = re.search(
        r"\b(kota|kabupaten|kab\.?)\s+([a-z](?:[a-z\s]+[a-z])?)",
        query
    )
    if match:
        prefix = match.group(1)
        name   = match.group(2).strip()
        # FIX Bug 1: strip trailing single/double-char tokens (sisa akreditasi grade "a","b","c")
        tokens = name.split()
        while tokens and len(tokens[-1]) <= 2:
            tokens.pop()
        name = " ".join(tokens).strip()
        if name:
            return f"{prefix} {name}"

    # 3. Pola kota + arah
    match = re.search(
        r"\b(jakarta|bandung|bekasi|bogor|tangerang|surabaya|medan|makassar)"
        r"\s+(selatan|barat|timur|utara|pusat)\b",
        query
    )
    if match:
        return f"{match.group(1)} {match.group(2)}"
 
    # 4. COMMON_CITIES
    for city in sorted(COMMON_CITIES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(city)}\b", query):
            return city
 
    # 5. KNOWN_DISTRICTS
    for district in sorted(KNOWN_DISTRICTS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(district)}\b", query):
            return district
 
    return None
 
def extract_wilayah_from_index(query: str, force_level: str = None) -> tuple:
    """
    Deteksi wilayah dari LOCATION_INDEX sebagai fallback.
    
    Guard rules:
    1. Jika hanya 1 token non-stopword tersisa:
       - Jika ada di KECAMATAN/KABUPATEN → ambil sebagai wilayah
         (user menyebut kecamatan/kab sebagai lokasi)
       - Jika hanya di DESA → skip (kemungkinan nama sekolah)
    2. Jika 2+ token → cari dari AKHIR query (nama sekolah di depan)
    """
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        return (None, None)
 
    q = re.sub(r"[^\w\s]", " ", query.lower()).strip()
 
    stopwords = set(STOPWORDS) | {
        "sd", "smp", "sma", "smk", "ma", "mts", "mi", "tk", "ra",
        "negeri", "swasta", "sdn", "smpn", "sman", "smkn", "man",
        "mtsn", "min", "it", "terpadu", "islam", "kecamatan", "kec",
        "kabupaten", "kab", "kota", "provinsi", "prov", "desa",
        "kelurahan", "negara",
        # Tambahan: typo variants yang sering muncul di query
        # Jenjang PAUD dan kata area generik
        "paud", "sps", "tpa", "paudq", "sdtk",
        # Kata area generik yang bukan nama wilayah
        "daerah", "wilayah", "area", "kawasan", "sekitar",
        # Ini perlu agar token typo tidak dihitung sebagai lokasi
        "prfil", "profl", "profil", "seklh", "sekolh", "sekola",
        "seklah", "sekolah", "nma", "nam",
    }
    tokens = [t for t in q.split() if t not in stopwords and len(t) >= 3]
 
    if not tokens:
        return (None, None)
 
    kab_lookup = LOCATION_INDEX.get("kabupaten", {})
    kec_lookup = LOCATION_INDEX.get("kecamatan", {})
    desa_lookup = LOCATION_INDEX.get("desa_kelurahan", {})
 
    if force_level:
        all_levels = ["kabupaten", "kecamatan", "desa_kelurahan"]
        check_order = [force_level] + [l for l in all_levels if l != force_level]
    else:
        check_order = ["kabupaten", "kecamatan", "desa_kelurahan"]
 
    # ── Kasus 1 token ────────────────────────────────────────────
    if len(tokens) == 1:
        token = tokens[0]
        # Hanya ambil sebagai wilayah jika ada di KABUPATEN
        # Kecamatan sering sama dengan nama desa/sekolah → skip
        in_kab = (token in kab_lookup
                  or f"kab. {token}" in kab_lookup
                  or f"kota {token}" in kab_lookup)
        if in_kab:
            return (token, "kabupaten")
        # 1-token kecamatan/desa → skip (kemungkinan nama sekolah)
        return (None, None)
 
    # ── Kasus 2+ token: cari dari AKHIR ──────────────────────────
    for level in check_order:
        lookup = LOCATION_INDEX.get(level, {})
        if not lookup:
            continue
 
        for n in range(min(3, len(tokens)), 0, -1):
            for i in range(len(tokens) - n, -1, -1):
                phrase = " ".join(tokens[i:i+n])
 
                # Guard: jangan ambil jika ini adalah token pertama
                # dan tidak ada token lain (sudah dicek di 1-token case)
                remaining_after = tokens[i+n:]
                remaining_before = tokens[:i]
 
                # Jika phrase mencakup semua token → skip
                if not remaining_before and not remaining_after:
                    continue
 
                if (phrase in lookup
                        or f"kec. {phrase}" in lookup
                        or f"kab. {phrase}" in lookup
                        or f"kota {phrase}" in lookup):
                    return (phrase, level)
 
    return (None, None)
 
def extract_wilayah_extra_from_index(query: str, wilayah_primary: Optional[str]) -> Optional[str]:
    """
    Setelah wilayah utama terdeteksi, cari token lokasi sekunder
    (biasanya desa/kelurahan untuk disambiguasi).
    Pencarian dari akhir query untuk menghindari nama sekolah.
    """
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        return None
 
    if not wilayah_primary:
        return None
 
    q = re.sub(r"[^\w\s]", " ", query.lower()).strip()
    q = re.sub(rf"\b{re.escape(wilayah_primary.lower())}\b", "", q)
 
    stopwords = set(STOPWORDS) | {
        "sd", "smp", "sma", "smk", "ma", "mts", "mi", "tk", "ra",
        "negeri", "swasta", "sdn", "smpn", "sman", "smkn", "man",
        "mtsn", "min", "it", "terpadu", "islam", "kecamatan", "kec",
        "kabupaten", "kab", "kota", "provinsi", "prov", "desa",
        "kelurahan", "negara",
        # Tambahan: typo variants yang sering muncul di query
        # Ini perlu agar token typo tidak dihitung sebagai lokasi
        "prfil", "profl", "profil", "seklh", "sekolh", "sekola",
        "seklah", "sekolah", "nma", "nam",
    }
    tokens = [
        t for t in q.split()
        if t not in stopwords and not t.isdigit() and len(t) >= 4
    ]
 
    if not tokens:
        return None
 
    desa_lookup = LOCATION_INDEX.get("desa_kelurahan", {})
    kec_lookup  = LOCATION_INDEX.get("kecamatan", {})
 
    # Cari dari akhir query (konvensi: nama sekolah di depan, lokasi di belakang)
    for n in range(min(3, len(tokens)), 0, -1):
        for i in range(len(tokens) - n, -1, -1):  # ← dari akhir ke awal
            phrase = " ".join(tokens[i:i+n])
            if (phrase in desa_lookup
                    or phrase in kec_lookup
                    or f"kec. {phrase}" in kec_lookup):
                return phrase
 
    return None
 
def extract_wilayah_extra(query: str, wilayah_primary: Optional[str]) -> Optional[str]:
    """
    Ekstrak token lokasi sekunder dari query setelah wilayah utama diambil.
    Berguna untuk disambiguasi sub-lokasi seperti desa/kelurahan.
    Contoh: query="sma negeri 1 cisarua jambudipa"
      → wilayah_primary="cisarua"
      → wilayah_extra="jambudipa"  ← nama desa yang membedakan
    """
    if not wilayah_primary:
        return None
 
    # Hapus wilayah utama dari query
    q = query
    wilayah_clean = re.sub(
        r"^(kota|kabupaten|kab\.?)\s+", "", wilayah_primary.lower()
    ).strip()
    q = re.sub(rf"\b{re.escape(wilayah_clean)}\b", "", q)
    q = re.sub(rf"\b{re.escape(wilayah_primary.lower())}\b", "", q)
 
    # Hapus stopwords dan jenis sekolah
    stopwords_extra = set(STOPWORDS) | {
        "sd", "smp", "sma", "smk", "ma", "mts", "mi", "tk", "ra",
        "negeri", "swasta", "sdn", "smpn", "sman", "smkn", "man",
        "mtsn", "min", "it", "terpadu", "islam", "negara",
    }
    tokens = [
        t for t in re.sub(r"[^\w\s]", " ", q).split()
        if t not in stopwords_extra
        and not t.isdigit()
        and len(t) >= 4
    ]
 
    if not tokens:
        return None
 
    # Cek apakah token yang tersisa terlihat seperti nama lokasi
    # (bukan bagian dari nama sekolah) — heuristik: token paling akhir
    # yang tidak mengandung angka biasanya adalah nama lokasi
    for token in reversed(tokens):
        # Skip token yang kemungkinan nama sekolah (sudah ada di school_name)
        if token.isalpha() and len(token) >= 4:
            return token
 
    return None
 
# =====================================================
# CLEAN SCHOOL NAME
# =====================================================
 
def clean_school_name(query: str, wilayah: str | None = None) -> str:
 
    q = query
 
    q = re.sub(r"\b(kota|kabupaten|kab\.?)\s+[a-z\s]+", "", q)
 
    for alias in WILAYAH_ALIAS:
        q = re.sub(rf"\b{alias}\b", "", q)
 
    if wilayah:
        wilayah_lower    = wilayah.lower()
        wilayah_stripped = re.sub(
            r"^(kota|kabupaten|kab\.?)\s+", "", wilayah_lower
        ).strip()
 
        is_city_level = (
            wilayah_lower != wilayah_stripped
            or wilayah_stripped in [c.lower() for c in COMMON_CITIES]
        )
 
        if is_city_level:
            q = re.sub(rf"\b{re.escape(wilayah_stripped)}\b", "", q)
            q = re.sub(rf"\b{re.escape(wilayah_lower)}\b", "", q)
 
    for city in sorted(COMMON_CITIES, key=len, reverse=True):
        q = re.sub(rf"\b{re.escape(city)}\b", "", q)
 
    tokens   = q.split()
    filtered = [t for t in tokens if t not in DIRECTION_WORDS]
    q = " ".join(filtered)
 
    q = re.sub(r"\s+", " ", q)
 
    return q.strip()
 
 
# =====================================================
# NORMALIZE SCHOOL NAME
# =====================================================
 
def normalize_school_name(name: str) -> str:
    name = name.lower()
 
    replacements = {
        "sma negeri": "sma negeri",
        "smk negeri": "smk negeri",
        "smp negeri": "smp negeri",
        "sd negeri":  "sd negeri",
        "ma negeri":  "ma negeri",
        "mts negeri": "mts negeri",
        "mi negeri":  "mi negeri",
    }
 
    for prefix, value in replacements.items():
        if name.startswith(prefix):
            return value + name[len(prefix):]
 
    return name
 
 
# =====================================================
# MAIN QUERY PARSER
# =====================================================
 
def extract_akreditasi(query: str) -> Optional[str]:
    """
    Ekstrak nilai akreditasi dari query.
    Return "A", "B", "C", "Tidak Terakreditasi", atau None.

    Contoh:
        "akreditasinya A"                  → "A"
        "sekolah negeri terakreditasi A"   → "A"
        "akreditasi B di jakarta"          → "B"
        "sma akreditasi a+"               → "A"
        "belum terakreditasi di padang"    → "Tidak Terakreditasi"
        "tidak terakreditasi"              → "Tidak Terakreditasi"
    """
    q = query.lower()

    # FIX Bug 2: Deteksi "belum / tidak terakreditasi" DULU sebelum pattern A/B/C
    # agar "belum terakreditasi" tidak ter-parse sebagai None
    if re.search(
        r'\b(belum|tidak|tanpa|non)\s*(terakreditasi|berakreditasi|akreditasi)\b',
        q
    ):
        return "Tidak Terakreditasi"

    # Guard: jika ini pola "B ke atas" / "minimal B" → return None,
    # biarkan extract_akreditasi_filter yang menanganinya sebagai gte
    if re.search(
        r'\b(?:akreditasi\w*|terakreditasi)\s+[abc]\s*(?:ke\s*atas|keatas|atau\s*lebih)',
        q
    ):
        return None
    if re.search(r'\b(?:minimal|minimum|setidaknya)\s+(?:akreditasi\s+)?[abc]\b', q):
        return None

    # Pattern: "akreditasi[nya] X" atau "terakreditasi X" atau "berakreditasi X"
    # Tambah \w* agar cocok dengan "akreditasinya"
    match = re.search(
        r'\b(?:akreditasi\w*|terakreditasi|berakreditasi)\s+([abc])\b',
        q
    )
    if match:
        return match.group(1).upper()

    # Pattern singkat: "akred a", "akred b"
    match = re.search(r'\bakred\s+([abc])\b', q)
    if match:
        return match.group(1).upper()

    # Pattern: "nilai a", "predikat a" (dalam konteks akreditasi)
    if re.search(r'\bakreditasi\w*\b', q):
        match = re.search(r'\b([abc])\b', q)
        if match:
            return match.group(1).upper()

    return None
 
def extract_akreditasi_filter(query: str) -> Optional[dict]:
    """
    Ekstrak filter akreditasi dari query.
    Return dict dengan mode dan nilai, atau None.
    
    Contoh:
        "terakreditasi B ke atas"  → {"mode": "gte", "values": ["A", "B"]}
        "akreditasi A"             → {"mode": "exact", "values": ["A"]}
        "minimal akreditasi B"     → {"mode": "gte", "values": ["A", "B"]}
    """
    q = query.lower()
    
    # Pattern "B ke atas", "minimal B", "minimum B", "≥ B"
    match = re.search(
        r'\b(?:akreditasi|terakreditasi|berakreditasi)\s+([abc])\s+(?:ke\s+atas|keatas|atau\s+lebih|keatas)',
        q
    )
    if not match:
        match = re.search(
            r'\b(?:minimal|minimum|paling\s+rendah|setidaknya)\s+(?:akreditasi\s+)?([abc])\b',
            q
        )
    
    if match:
        base = match.group(1).upper()
        # "B ke atas" berarti A dan B
        # "C ke atas" berarti A, B, dan C
        all_grades = ["A", "B", "C"]
        idx = all_grades.index(base)
        values = all_grades[:idx + 1]  # A=0, B=1, C=2 → ambil dari 0 sampai idx
        return {"mode": "gte", "values": values}
    
    # Exact match biasa
    exact = extract_akreditasi(query)
    if exact:
        return {"mode": "exact", "values": [exact]}
    
    return None
 
def parse_school_query(query: str) -> Dict:
    """
    Parse query dan return dict dengan keys:
      school_name, bentuk, wilayah, wilayah_extra, year, number, status
    """
 
    # 1. Deteksi bentuk + status dari query MENTAH
    bentuk_raw, status_raw = detect_bentuk_and_status(query)
 
    # 2. Normalisasi
    normalized = normalize_query(query)
 
    # 3. Ekstraksi field
    year    = extract_year(normalized)
    number  = extract_school_number(normalized)
    wilayah = extract_wilayah(normalized)
 
    # Fallback: gunakan LOCATION_INDEX jika extract_wilayah gagal
    if not wilayah:
        # FIX 2: jika ada hint level "kecamatan" di query asli,
        # paksa cari di level kecamatan dulu (hindari desa salah deteksi)
        has_kec_hint = bool(re.search(r"\b(kecamatan|kec)\b", query.lower()))
        wilayah_idx, _ = extract_wilayah_from_index(normalized, force_level="kecamatan" if has_kec_hint else None)
        if wilayah_idx:
            wilayah = wilayah_idx
 
    # Ekstrak wilayah sekunder untuk disambiguasi
    wilayah_extra = extract_wilayah_extra_from_index(normalized, wilayah)
 
    # 4. detect_school_type
    bentuk_norm = detect_school_type(normalized)
    bentuk = bentuk_raw or bentuk_norm
 
    # 5. Status
    status = status_raw
    if not status:
        tokens = set(normalized.split())
        if tokens & NEGERI_INDICATORS:
            status = "NEGERI"
 
    # 6. Bersihkan school_name
    school_name = clean_school_name(normalized, wilayah)
 
    # FIX 1: strip wilayah dari index (yang tidak ada di COMMON_CITIES)
    # dari school_name
    if wilayah and school_name:
        wilayah_clean = re.sub(
            r"^(kota|kabupaten|kab\.?|kecamatan|kec\.?)\s+", "",
            wilayah.lower()
        ).strip()
        school_name = re.sub(
            rf"\b{re.escape(wilayah_clean)}\b", "", school_name
        ).strip()
        school_name = re.sub(r"\s+", " ", school_name).strip()
 
 
    school_name = normalize_school_name(school_name)
    
    akreditasi = extract_akreditasi(query)
    
    return {
        "school_name":   school_name,
        "bentuk":        bentuk,
        "wilayah":       wilayah,
        "wilayah_extra": wilayah_extra,
        "year":          year,
        "number":        number,
        "status":        status,
        "akreditasi":     akreditasi,
    }
    
_PAUD_JENJANG_EXPAND = [
    "TK", "KB", "RA", "SPS", "TPA", "PAUDQ",
    "SPK TK", "SPK KB", "SDTK",
]
def extract_query_filters(query: str) -> Dict[str, Any]:
    """
    Extract semua filter operasional dari query untuk aggregation & comparison.
    Return dict kosong jika tidak ada filter.
 
    Contoh:
        "berapa SMA di sleman terakreditasi B ke atas" →
            {"bentuk_pendidikan": "SMA", "akreditasi_gte": ["A", "B"]}
        "bandingkan surabaya vs gresik jenjang SMA" →
            {"bentuk_pendidikan": "SMA"}
        "berapa SD negeri di sleman" →
            {"bentuk_pendidikan": "SD", "status_sekolah": "NEGERI"}
    """
    q = query.lower()
    result = {}
    
    if re.search(r'\bpaud\b', q):
        result["bentuk_pendidikan_in"] = _PAUD_JENJANG_EXPAND
        result["jenjang_requested"] = _PAUD_JENJANG_EXPAND.copy()
    # ── Jenjang tunggal ──────────────────────────────────────────────────────
    # Urutan penting: cek yang lebih spesifik dulu (SMA/SMK sebelum MA/MI)
    _JENJANG_MAP = [
        (r"\bsmk\b",  "SMK"),
        (r"\bsma\b",  "SMA"),
        (r"\bsmp\b",  "SMP"),
        (r"\bsd\b",   "SD"),
        (r"\bmts\b",  "MTs"),
        (r"\bma\b",   "MA"),
        (r"\bmi\b",   "MI"),
        (r"\btk\b",   "TK"),
        (r"\bkb\b",   "KB"),
        (r"\bra\b",   "RA"),
        (r"\bsps\b",  "SPS"),
        (r"\btpa\b",  "TPA"),
    ]
    # ── Jenjang tunggal (dengan guard multi-jenjang) ────────────────────────────
    jenjang_found = []
 
    for pattern, jenjang in _JENJANG_MAP:
        if re.search(pattern, q):
            jenjang_found.append(jenjang)
 
    # PENTING: hanya inject jika TEPAT SATU jenjang
    if len(jenjang_found) == 1 and "bentuk_pendidikan_in" not in result:
        result["bentuk_pendidikan"] = jenjang_found[0]
 
    # Selalu simpan daftar lengkap untuk narrator
    if jenjang_found:
        result.setdefault("jenjang_requested", []).extend(jenjang_found)
        
    # ── Status sekolah ───────────────────────────────────────────────────────
    if re.search(r"\bnegeri\b", q):
        result["status_sekolah"] = "NEGERI"
    elif re.search(r"\bswasta\b", q):
        result["status_sekolah"] = "SWASTA"

    # ── Akreditasi ───────────────────────────────────────────────────────────
    # "B ke atas" / "minimal B" → ambil semua grade ≥ B
    match_gte = re.search(
        r'\b(?:akreditasi\w*|terakreditasi)\s+([abc])\s*(?:ke\s*atas|keatas|atau\s*lebih)',
        q
    )
    if not match_gte:
        match_gte = re.search(
            r'\b(?:minimal|minimum|setidaknya)\s+(?:akreditasi\s+)?([abc])\b', q
        )
    if match_gte:
        base = match_gte.group(1).upper()
        all_grades = ["A", "B", "C"]
        idx = all_grades.index(base)
        result["akreditasi_gte"] = all_grades[:idx + 1]  # B ke atas → [A, B]
    else:
        # Exact match: "akreditasinya A"
        exact = extract_akreditasi(query)
        if exact:
            result["akreditasi_exact"] = exact

    # ── FIX Bug 4: Operator / Yayasan (nama penyelenggara) ───────────────────
    # Deteksi nama yayasan/penyelenggara yang umum agar bisa difilter di ES
    _OPERATOR_PATTERNS = [
        (r'\bmuhammadiyah\b',             "muhammadiyah"),
        (r'\bnahdlatul\s*ulama\b|\bnu\b', "nu"),
        (r'\bal.?azhar\b',                "al azhar"),
        (r'\bkristen\b',                  "kristen"),
        (r'\bkatolik\b',                  "katolik"),
        (r'\badvent\b',                   "advent"),
        (r'\bbudha\b|\bbuddha\b',         "budha"),
        (r'\bhindu\b',                    "hindu"),
        (r'\bpgri\b',                     "pgri"),
        (r'\bbi[_\s]?nur\b',              "binur"),
        (r'\bpetrokimia\b',               "petrokimia"),
        (r'\bpertamina\b',                "pertamina"),
        (r'\btelkom\b',                   "telkom"),
        (r'\bpelita\s*harapan\b',         "pelita harapan"),
        (r'\bbudi\s*luhur\b',             "budi luhur"),
    ]
    for pattern, op_value in _OPERATOR_PATTERNS:
        if re.search(pattern, q):
            result["nama_operator"] = op_value
            break

    # ── FIX Bug 6: SPK (Satuan Pendidikan Kerjasama) ─────────────────────────
    # "spk sma", "sekolah spk", dll — filter nama sekolah mengandung "SPK"
    if re.search(r'\bspk\b', q):
        result["is_spk"] = True

    return result