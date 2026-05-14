# services/core/cognitive_router.py
"""
CognitiveRouter v2 — Single LLM call untuk classify + extract entities.
Menggantikan:
  - llm_intent_classifier.py (klasifikasi)
  - smart_query_parser.py (ekstraksi entitas rule-based)
"""
 
import re
import json
import logging
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum
 
from app.providers.deepseek import call_deepseek
from app.utils.cache import get_cache, set_cache
from app.utils.tiered_cache import TieredCache
from app.utils.prompt_loader import load_prompt
from app.services.core.oot_response_templates import get_oot_response

logger = logging.getLogger(__name__)

_intent_cache = TieredCache(maxsize=512)
 
_CACHE_TTL = 86_400  # 24 jam
# Harus LEBIH BESAR dari DeepSeek read_timeout supaya httpx yang timeout lebih dulu
# (error message lebih informatif dari asyncio.CancelledError yang generik)
_LLM_TIMEOUT = 65.0
 
 
# ════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ════════════════════════════════════════════════════════════════
 
@dataclass
class WilayahEntities:
    provinsi:  Optional[str] = None
    kabupaten: Optional[str] = None
    kecamatan: Optional[str] = None
    desa:      Optional[str] = None
    landmark:  Optional[str] = None
    # Phase 14: ambiguity support
    ambiguous:  bool = False
    candidates: list = field(default_factory=list)

@dataclass
class CognitiveIntent:
    # Routing
    module:    str = "out_of_context"
    secondary: List[str] = field(default_factory=list)
    scope:     str = "nasional"
    sub_queries: Dict[str, str] = field(default_factory=dict)

    # Entities
    jenjang:          Optional[str] = None
    jenjang_list:     List[str] = field(default_factory=list)
    status:           Optional[str] = None
    akreditasi:       Optional[str] = None
    akreditasi_mode:  Optional[str] = None
    akreditasi_list:  List[str] = field(default_factory=list)
    
    nama_sekolah:     Optional[str] = None
    npsn:             Optional[str] = None
    wilayah:          WilayahEntities = field(default_factory=WilayahEntities)
    wilayah_bandingkan: Optional[str] = None
    yayasan:          Optional[str] = None
    koordinat_user:   bool = False
    radius_km:        Optional[float] = None
    metric_target: Optional[str]  = None   # "sekolah"|null — hanya hitung sekolah di index baru

    # QC filters
    qc_masalah_only:  bool = False          # hanya sekolah dengan total_masalah > 0
    qc_field:         Optional[int] = None  # kolom QC spesifik (1-13)
    qc_min_masalah:   Optional[int] = None  # filter minimum jumlah masalah
    sort_by:    Optional[str] = None   # "total_masalah" | "persen_bersih" | "akreditasi" | null
    sort_order: Optional[str] = None   # "asc" | "desc" | null
    qc_persen_min: Optional[float] = None  # filter persen_bersih >= nilai ini
    qc_persen_max: Optional[float] = None  # filter persen_bersih <= nilai ini
    qc_field_list: List[int] = field(default_factory=list)  # multi kondisi QC sekaligus
    qc_persen_query: bool = False  # flag: user minta perhitungan % sekolah bersih/bermasalah

    # Atribut sekolah (idx_sekolah_qc)
    keaktifan:         Optional[str]  = None  # "A" (aktif) atau "T" (tidak aktif)
    wilayah_terpencil: Optional[bool] = None
    wilayah_perbatasan: Optional[bool] = None

    # OOT
    is_oot:    bool = False
    oot_reason: Optional[str] = None

    # Dependency-aware orchestration
    dependencies: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Debug
    raw_label: Optional[str] = None
    @property
    def primary(self) -> str:
        """Alias .module → kompatibel dengan IntentResult.primary di rag_server."""
        return self.module
 
    @primary.setter
    def primary(self, value: str) -> None:
        self.module = value
 
    @property
    def filters(self) -> dict:
        """Alias dict sederhana untuk akses filter — kompatibel dengan test & IntentResult.filters."""
        result = {}
        if self.jenjang:
            result["jenjang"] = self.jenjang
        if self.status:
            result["status"] = self.status
        if self.akreditasi:
            result["akreditasi"] = self.akreditasi
        if self.nama_sekolah:
            result["nama_sekolah"] = self.nama_sekolah
        if self.npsn:
            result["npsn"] = self.npsn
        if self.yayasan:
            result["yayasan"] = self.yayasan
        if self.radius_km is not None:
            result["radius_km"] = self.radius_km
        return result
 
VALID_MODULES = {
    "aggregation", "comparison", "profile", "list", "geo", "out_of_context"
}
 
VALID_SCOPES = {
    "nasional", "provinsi", "kabupaten", "kecamatan", "desa", "geo_radius"
}
 
class QueryRoute(str, Enum):
    AGGREGATION    = "aggregation"
    COMPARISON     = "comparison"
    PROFILE        = "profile"
    LIST           = "list"
    GEO            = "geo"
    OUT_OF_CONTEXT = "out_of_context"
 
# ════════════════════════════════════════════════════════════════
# SYSTEM PROMPT (import dari file terpisah untuk kemudahan editing)
# ════════════════════════════════════════════════════════════════
 
# Load prompt dari file eksternal — edit prompts/intent_classifier/system_v1.txt
# untuk mengubah prompt tanpa menyentuh kode ini.
_SYSTEM_PROMPT: str = load_prompt("intent_classifier")
 
 
# ════════════════════════════════════════════════════════════════
# KEYWORD FALLBACK (saat LLM tidak tersedia)
# ════════════════════════════════════════════════════════════════
 
_OOC_STRONG = {
    "presiden", "gubernur", "bupati", "walikota", "pilpres", "pilkada",
    "saham", "ihsg", "bitcoin", "crypto", "forex", "investasi",
    "film", "netflix", "spotify", "resep", "masakan", "traveling", "hotel",
    "cuaca", "gempa", "tsunami", "covid", "vaksin", "dokter", "rumah sakit",
    "israel", "palestina", "korea", "inggris", "amerika",
}
 
_SCHOOL_SIGNALS = {
    "sekolah", "pendidikan", "siswa", "guru", "kelas", "kurikulum",
    "sd", "smp", "sma", "smk", "mi", "mts", "ma", "tk", "paud", "slb",
    "sman", "smpn", "sdn", "smkn", "man", "mtsn", "min",
    "npsn", "akreditasi", "ppdb", "zonasi", "honorer",
    # Infrastruktur satuan pendidikan
    "jamban", "toilet", "sanitasi", "air bersih", "sumber air",
    "listrik", "internet", "komputer", "kode pos",
    # Kualitas data & atribut index baru
    "kualitas data", "data bermasalah", "koordinat", "npsn ganda",
    "sk izin", "sk pendirian", "foto sekolah",
    "wilayah terpencil", "wilayah perbatasan", "bos", "keaktifan sekolah",
    # Kualitas data / residu
    "residu", "koordinat kosong",
    "npsn ganda", "persen bersih", "total masalah", "masalah data",
    "yayasan tidak terdaftar",
}
 
_WILAYAH_ALIAS = {
    # Jakarta
    "tangsel":  "tangerang selatan",
    "jaksel":   "jakarta selatan",
    "jakbar":   "jakarta barat",
    "jakpus":   "jakarta pusat",
    "jakut":    "jakarta utara",
    "jakti":    "jakarta timur",
    # Yogyakarta
    "jogja":    "yogyakarta",
    "diy":      "yogyakarta",
    "yogya":    "yogyakarta",
    # Jawa
    "jabar":    "jawa barat",
    "jatim":    "jawa timur",
    "jateng":   "jawa tengah",
    # Kalimantan
    "kaltim":   "kalimantan timur",
    "kalbar":   "kalimantan barat",
    "kalteng":  "kalimantan tengah",
    "kalsel":   "kalimantan selatan",
    "kaltara":  "kalimantan utara",
    # Sulawesi
    "sulsel":   "sulawesi selatan",
    "sulut":    "sulawesi utara",
    "sulteng":  "sulawesi tengah",
    "sulbar":   "sulawesi barat",
    "sultra":   "sulawesi tenggara",
    # Nusa Tenggara
    "ntt":      "nusa tenggara timur",
    "ntb":      "nusa tenggara barat",
    # Sumatera
    "sumut":    "sumatera utara",
    "sumbar":   "sumatera barat",
    "sumsel":   "sumatera selatan",
    # Lainnya
    "solo":     "surakarta",
    "bdg":      "bandung",
    "sby":      "surabaya",
}
 
_AKRED_RE = re.compile(
    r'\b(?:akreditasi|terakreditasi|berakreditasi|terkareditasi|'
    r'terakred|akred)\w*\s+([abc])\b',
    re.I
)
_JENJANG_RE = re.compile(
    r'\b(sd|smp|sma|smk|mi|mts|ma|tk|paud|kb|ra|sps|tpa|paudq|pkbm|skb|kursus|lkp|slb)\b'
    r'|(?:pondok\s+pesantren|pesantren|pondok|universitas|institut|politeknik|akademi|sekolah\s+tinggi)',
    re.I
)
_STATUS_RE  = re.compile(r'\b(negeri|swasta)\b', re.I)
_NPSN_RE    = re.compile(r'\b(\d{8})\b')
_YAYASAN_RE = re.compile(
    r'\b(?:milik|penyelenggara|yayasan|naungan)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b',
    re.I
)
_NAMED_SCHOOL_RE = re.compile(
    r'\b(sman|smpn|sdn|smkn|man|mtsn|min|tkn)\b', re.I
)
_NAMED_SCHOOL_MAP = {
    "sman": ("SMA", "NEGERI"), "smpn": ("SMP", "NEGERI"),
    "sdn":  ("SD",  "NEGERI"), "smkn": ("SMK", "NEGERI"),
    "man":  ("MA",  "NEGERI"), "mtsn": ("MTs", "NEGERI"),
    "min":  ("MI",  "NEGERI"), "tkn":  ("TK",  "NEGERI"),
}
 
def _keyword_fallback(query: str) -> CognitiveIntent:
    """
    Keyword fallback sederhana — digunakan saat LLM tidak tersedia.
    Tidak perlu se-komprehensif dulu karena LLM seharusnya handle 95%+ kasus.
    """
    q      = query.lower()
    tokens = set(q.split())
    import re as _re
 
    # Multi-jenjang detection
    _JENJANG_MAP = {
        "sd": "SD", "mi": "MI", "smp": "SMP", "mts": "MTs",
        "sma": "SMA", "smk": "SMK", "ma": "MA",
        "tk": "TK", "paud": "PAUD", "slb": "SLB",
        "kb": "KB", "ra": "RA", "sps": "SPS", "tpa": "TPA",
        "paudq": "PAUDQ", "pkbm": "PKBM", "skb": "SKB",
        "kursus": "Kursus", "lkp": "LKP",
        "pesantren": "Pondok Pesantren",
        "pondok pesantren": "Pondok Pesantren",
        "pondok": "Pondok Pesantren",
        "universitas": "Universitas",
        "institut": "Institut",
        "politeknik": "Politeknik",
        "akademi": "Akademi",
        "sekolah tinggi": "Sekolah Tinggi",
        # Prioritas tinggi
        "slb":              "SLB",
        "luar biasa":       "SLB",
        "akademik":         "Akademik",
        "spk":              "SPK SMA",   # default SPK tanpa suffix → SMA
        "spk sd":           "SPK SD",
        "spk smp":          "SPK SMP",
        "spk sma":          "SPK SMA",
        "spk smk":          "SPK SMK",
        "spk tk":           "SPK TK",
        "spk kb":           "SPK KB",
        "smtk":             "SMTK",
        "smptk":            "SMPTK",
        "spm ulya":         "SPM Ulya",
        "spm wustha":       "SPM Wustha",
        "spm ula":          "SPM Ula",
        "pratama widyalaya":"Pratama Widyalaya",
        # Prioritas medium
        "sdtk":             "SDTK",
        "taman seminari":   "Taman Seminari",
        "seminari":         "Taman Seminari",
        "smak":             "SMAK",
        "smag":             "SMAg.K",
        "akademi komunitas":"Akademi Komunitas",
        "nava dhammasekha": "Nava Dhammasekha",
        "pdf ulya":         "PDF Ulya",
        "pdf wustha":       "PDF Wustha",
        "pdf":              "PDF Ulya",   # ambiguous, default ulya
        "widyalaya":        "Pratama Widyalaya",  # default paling umum
        "mak":              "MAK",
    }
 
    def _match_jenjang_key(key: str, text: str) -> bool:
        """Handle single-word (\\b) dan multi-word (spasi) key."""
        if ' ' in key:
            # Multi-word: cukup substring setelah normalisasi spasi
            return key in text
        return bool(_re.search(r'\b' + _re.escape(key) + r'\b', text))

    _found_jenjang = [
        v for k, v in _JENJANG_MAP.items()
        if _match_jenjang_key(k, q)
    ]
 
    _jenjang_single = _found_jenjang[0] if len(_found_jenjang) == 1 else None
    _jenjang_multi  = _found_jenjang    if len(_found_jenjang) > 1 else []
    has_ooc    = any(w in q for w in _OOC_STRONG)
    has_school = (
        any(w in tokens for w in _SCHOOL_SIGNALS)
        or bool(re.search(r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk)n?\b', q))
        or any(_match_jenjang_key(k, q) for k in _JENJANG_MAP)
    )
 
    if has_ooc and not has_school:
        return CognitiveIntent(module="out_of_context", is_oot=True)
 
    # Ekstrak entitas dasar (ENHANCED AKREDITASI)
    _akred = None
    _akred_mode = None
 
    # Pola GTE akreditasi (B ke atas, minimal B, dst)
    _akred_gte_pattern = _re.compile(
        r'\b(akreditasi|terakreditasi|berakreditasi)\s+([abc])\s+(ke\s+atas|keatas|dan\s+di\s+atas|minimal|minimum)',
        _re.IGNORECASE
    )
 
    # Pola "A dan B"
    _akred_ab_pattern = _re.compile(
        r'\bakreditasi\s+[ab]\s+dan\s+[ab]\b',
        _re.IGNORECASE
    )
 
    m_gte = _akred_gte_pattern.search(q)
    if m_gte:
        _akred = m_gte.group(2).upper()
        _akred_mode = "gte"
    elif _akred_ab_pattern.search(q):
        _akred = "B"  # Ambil yang paling rendah → include A & B
        _akred_mode = "gte"
    elif _re.search(r'\bakreditasi\s+([abc])\b', q):
        m = _re.search(r'\bakreditasi\s+([abc])\b', q)
        _akred = m.group(1).upper()
        _akred_mode = "exact"
 
    # fallback ke regex lama kalau belum kena
    if not _akred:
        m = _AKRED_RE.search(q)
        if m:
            _akred = m.group(1).upper()
            _akred_mode = "exact"
 
    akreditasi = _akred
 
    jenjang = status = None
 
    m = _NAMED_SCHOOL_RE.search(q)
    if m:
        jenjang, status = _NAMED_SCHOOL_MAP[m.group(1).lower()]
    else:
        # Gunakan hasil multi-jenjang detection
        if _jenjang_single:
            jenjang = _jenjang_single
        else:
            jenjang = None  # multi → kosongkan
 
        # Status tetap pakai regex lama
        m = _STATUS_RE.search(q)
        if m:
            status = m.group(1).upper()
 
    npsn = None
    m = _NPSN_RE.search(q)
    if m: npsn = m.group(1)
 
    yayasan = None
    m = _YAYASAN_RE.search(query)
    if m: yayasan = m.group(1).strip()
 
    # ── Deteksi secondary (eksplisit double-intent) ──────────────────────────
    _SECONDARY_LIST_SIGNALS = (
        "sebutkan juga", "tampilkan juga", "dan tampilkan", "tampilkan daftar",
        "daftar nama", "nama-namanya", "dan lihat juga", "cari juga",
        "lokasinya", "daftarnya", "dan daftar",
    )
    _SECONDARY_GEO_SIGNALS = (
        "terdekat dari", "terdekat di",
    )
    secondary = []
    for sig in _SECONDARY_LIST_SIGNALS:
        if sig in q:
            secondary.append("list")
            break
    for sig in _SECONDARY_GEO_SIGNALS:
        if sig in q and "dari sini" not in q and "posisi" not in q and "koordinat" not in q:
            if "secondary" not in locals() or "geo" not in secondary:
                secondary.append("geo")
            break
 
    # ── Routing utama ────────────────────────────────────────────────────────
    # Prioritas infrastruktur: query tentang fasilitas sekolah (jamban, toilet, air, listrik, dst.)
    # → profile jika ada indikasi nama sekolah, list jika tidak
    _INFRA_SIGNALS = ("jamban", "toilet", "sanitasi", "air bersih", "sumber air",
                      "listrik", "internet", "komputer")
    _has_infra_signal = any(sig in q for sig in _INFRA_SIGNALS)

    # Prioritas 1: nama sekolah spesifik (SMPN 5, SDN 02, ...) atau NPSN
    # Pola jenjang untuk deteksi nama sekolah bernomor (SDN 5, PKBM 02, SLB 1, ...)
    _NUMBERED_SCHOOL_RE = re.compile(
        r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk|slb|pkbm|smtk|smak|sps|skb|ra|kb|tpa|paud)n?\s*\d+\b',
        re.IGNORECASE,
    )
    _has_named_school = bool(npsn or _NUMBERED_SCHOOL_RE.search(q))

    # Prioritas 2: query dengan nama sekolah non-numbered + kata profil/alamat/lokasi
    _PROFILE_SIGNALS = ("profil ", "profile ", "alamat ", "lokasi", "lokasinya", "di mana", "dimana", "terakreditasi", "akreditasi?")
    _has_profile_signal = any(sig in q for sig in _PROFILE_SIGNALS)

    # Dua nama sekolah bernomor + vs → profile (school vs school)
    _named_school_count = len(_NUMBERED_SCHOOL_RE.findall(q))
 
    if _has_named_school:
        module = "profile"

    elif _has_infra_signal and (_has_named_school or (jenjang and nama_sekolah_hint(q))):
        # Query infrastruktur sekolah dengan nama spesifik → profile
        module = "profile"

    elif _has_infra_signal and wilayah_hint(q):
        # Query infra tanpa nama sekolah tapi ada wilayah → list dengan field infra
        module = "list"

    elif _has_profile_signal and jenjang and nama_sekolah_hint(q):
        # Ada jenjang + sinyal profil + nama sekolah non-numbered → profile
        module = "profile"

    elif any(w in q for w in (
        "bandingkan", "versus", "dibanding", "perbandingan",
        "perbedaan", "selisih", "mana yang lebih",
        "mana lebih", "lebih banyak"
    )):
        module = "comparison"

    elif "vs" in q.split():
        # "vs" standalone — cek apakah dua nama sekolah spesifik
        module = "comparison"

    elif any(w in q for w in ("berapa", "jumlah", "total")) or any(
        abbr in q for abbr in ("jmlh", "jml ", "jml.")
    ) or re.search(r'\bpaling\s+banyak\b', q) or re.search(r'\bbanyak\s+sekolah\b', q):
        module = "aggregation"

    elif any(w in q for w in (
        "terdekat dari sini", "dari sini",
        "posisi saya", "posisi saya sekarang",
        "koordinat_user",
        "dari rumah saya", "dari rumah",
        "dari tempat saya",
    )) or re.search(r'\bradius\b', q) or re.search(r'\bkoordinat\b', q):
        module = "geo"

    elif any(w in q for w in ("daftar", "list", "tampilkan", "cari", "apa saja")):
        module = "list"

    elif has_school:
        module = "list"

    else:
        return CognitiveIntent(module="out_of_context", is_oot=True)
 
    # Hapus secondary yang sama dengan primary
    secondary = [s for s in secondary if s != module]

    # ═══════════════════════════════════════════════════════════════
    # SCOPE DETECTION — heuristik berdasarkan keyword wilayah
    # ═══════════════════════════════════════════════════════════════
    _scope = "nasional"

    _DESA_PATTERNS    = [r'\bdesa\s+\w+', r'\bkelurahan\s+\w+', r'\bkel\.\s*\w+']
    _KEC_PATTERNS     = [r'\bkecamatan\s+\w+', r'\bkec\.\s*\w+']
    _KAB_PATTERNS     = [r'\bkabupaten\s+\w+', r'\bkab\.\s*\w+', r'\bkota\s+\w+']
    _PROV_PATTERNS    = [r'\bprovinsi\s+\w+', r'\bprov\.\s*\w+']
    _NASIONAL_PATTERNS = [
        r'\bindonesia\b', r'\bnasional\b',
        r'\bse-?indonesia\b', r'\bseluruh\s+indonesia\b', r'\bnusantara\b',
    ]

    if any(_re.search(p, q) for p in _DESA_PATTERNS):
        _scope = "desa"
    elif any(_re.search(p, q) for p in _KEC_PATTERNS):
        _scope = "kecamatan"
    elif any(_re.search(p, q) for p in _KAB_PATTERNS):
        _scope = "kabupaten"
    elif any(_re.search(p, q) for p in _PROV_PATTERNS):
        _scope = "provinsi"
    elif any(_re.search(p, q) for p in _NASIONAL_PATTERNS):
        _scope = "nasional"
    else:
        # Fallback: ada " di <kata>" tapi tidak ada prefix administratif
        # → kemungkinan wilayah yang tidak dikenali, default ke kabupaten
        # agar TIDAK di-treat nasional oleh aggregation handler.
        _di_match = _re.search(r'\bdi\s+([a-z][a-z\s]+?)(?:\s|$|,|\.)', q)
        if _di_match:
            _possible_loc = _di_match.group(1).strip()
            _LOC_STOPWORDS = {
                "atas", "bawah", "dalam", "luar", "sini", "sana",
                "indonesia", "nusantara",
            }
            if _possible_loc and _possible_loc not in _LOC_STOPWORDS:
                _scope = "kabupaten"

    logger.debug("[KeywordFallback] scope=%s", _scope)

    # ═══════════════════════════════════════════════════════════════
    # SUB-QUERIES untuk multi-intent — splitting heuristik
    # ═══════════════════════════════════════════════════════════════
    _sub_queries: Dict[str, str] = {}

    if secondary:
        _CONNECTOR_PATTERN = _re.compile(
            r'\s*(?:,?\s*(?:dan|lalu|kemudian|serta|juga)\s+'
            r'(?=tampilkan|daftar|list|cari|sebutkan|tunjukkan|'
            r'bandingkan|berapa|jumlah|total|profil))',
            _re.IGNORECASE,
        )

        _parts = _CONNECTOR_PATTERN.split(query)
        _parts = [p.strip(" ,.") for p in _parts if p.strip()]

        _INTENT_KEYWORDS: Dict[str, list] = {
            "aggregation": ["berapa", "jumlah", "total", "banyak"],
            "list":        ["tampilkan", "daftar", "list", "cari",
                            "sebutkan", "tunjukkan"],
            "comparison":  ["bandingkan", "versus", "vs", "perbandingan",
                            "dibanding", "selisih"],
            "profile":     ["profil", "informasi tentang", "tentang sekolah",
                            "info sekolah"],
            "geo":         ["terdekat", "radius", "sekitar", "dari sini"],
        }

        if len(_parts) >= 2:
            _unassigned = [module] + list(secondary)
            for part in _parts:
                part_lower = part.lower()
                for mod in list(_unassigned):
                    if any(kw in part_lower for kw in _INTENT_KEYWORDS.get(mod, [])):
                        _sub_queries[mod] = part
                        _unassigned.remove(mod)
                        break
            # Modul yang tidak ter-assign fallback ke query asli
            for mod in _unassigned:
                _sub_queries[mod] = query
        else:
            # Tidak bisa split — semua modul pakai query asli
            _sub_queries[module] = query
            for sec in secondary:
                _sub_queries[sec] = query

        logger.debug("[KeywordFallback] sub_queries=%s", _sub_queries)

    # ═══════════════════════════════════════════════════════════════
    # DEPENDENCY DETECTION — ranking query that also requests a list
    # Pattern: terbanyak/tertinggi/terbesar/ranking + tampilkan/daftar/list/sekolahnya
    # → aggregation feeds list via top_location injection
    # ═══════════════════════════════════════════════════════════════
    _RANKING_DEP = {"terbanyak", "tertinggi", "terbesar"}
    _LIST_DEP    = {"tampilkan", "daftar", "list", "sekolahnya", "sebutkan"}
    _has_ranking_dep = any(w in q for w in _RANKING_DEP) or bool(_re.search(r'\branking\b', q))
    _has_list_dep    = any(w in q for w in _LIST_DEP)

    dependencies: Dict[str, Dict[str, Any]] = {}
    if _has_ranking_dep and _has_list_dep:
        module = "aggregation"
        if "list" not in secondary:
            secondary.append("list")
        dependencies = {
            "list": {
                "depends_on": "aggregation",
                "inject_from_result": {"provinsi": "top_location"},
            }
        }
        if not _sub_queries:
            _sub_queries = {"aggregation": query, "list": query}

    return CognitiveIntent(
        module=module,
        secondary=secondary,
        scope=_scope,
        sub_queries=_sub_queries,
        akreditasi=akreditasi,
        akreditasi_mode=_akred_mode,
        jenjang=jenjang,
        jenjang_list=_jenjang_multi,
        status=status,
        npsn=npsn,
        yayasan=yayasan,
        dependencies=dependencies,
    )
 
 
_GENERIC_AFTER_JENJANG = re.compile(
    r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk|slb|pkbm|smtk|smak|sps|skb|ra|kb|tpa|paud)\s+'
    r'(?:negeri|swasta|akreditasi|dan|atau|di|yang|apa|ini|itu|dengan|dalam|untuk|dari|vs|versus)\b',
    re.IGNORECASE
)
 
def wilayah_hint(q: str) -> bool:
    """Cek apakah ada indikasi wilayah/lokasi dalam query.

    Digunakan untuk membedakan query infra yang menyebut lokasi (→ list)
    dari query infra tanpa lokasi (→ fallback/profile).
    """
    _WILAYAH_MARKERS = (
        "kecamatan", "kabupaten", "provinsi", "desa ",
        "kelurahan", "kotamadya", " di ",
    )
    q_lower = q.lower() if not q.islower() else q
    return any(marker in q_lower for marker in _WILAYAH_MARKERS)


def nama_sekolah_hint(q: str) -> bool:
    """Cek apakah ada indikasi nama sekolah non-numbered (misal 'SMK Pelita Harapan').
    Jenjang harus diikuti nama proper, bukan kata umum/filter.
    """
    # Jika jenjang hanya diikuti kata-kata generik, bukan nama sekolah
    if _GENERIC_AFTER_JENJANG.search(q):
        return False
    # Jenjang diikuti huruf (nama proper)
    return bool(re.search(
        r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk|slb|pkbm|smtk|smak|sps|skb|ra|kb|tpa|paud)\s+[A-Za-z]',
        q, re.IGNORECASE
    ))
 
def _safe_float(v) -> Optional[float]:
    """Konversi nilai radius_km dari LLM output dengan aman."""
    if v is None or v in ("null", "", "None", "0"):
        return None
    try:
        result = float(v)
        return result if result > 0 else None
    except (ValueError, TypeError):
        return None
    
def _safe_int(v) -> Optional[int]:
    if v is None or v in ("null", "", "None"):
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None
# ════════════════════════════════════════════════════════════════
# LLM OUTPUT PARSER
# ════════════════════════════════════════════════════════════════

def _parse_llm_output(raw: str, query: str = "") -> Optional[CognitiveIntent]:
    if not raw:
        return None
    try:
        cleaned = re.sub(r'```(?:json)?\s*|\s*```', '', raw, flags=re.I).strip()
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if not m:
            return None
        obj = json.loads(m.group(0))

        module = obj.get("module", "").lower()
        if module not in VALID_MODULES:
            return None

        _scope_raw = obj.get("scope") or "nasional"   # handle null → default
        scope = str(_scope_raw).lower().strip()
        if scope not in VALID_SCOPES:
            scope = "nasional"

        secondary = [
            s.lower() for s in (obj.get("secondary") or [])
            if isinstance(s, str) and s.lower() in VALID_MODULES
        ]

        # Parse sub_queries dengan validasi
        _sub_queries_raw = obj.get("sub_queries") or {}
        sub_queries: Dict[str, str] = {}
        if isinstance(_sub_queries_raw, dict):
            for mod_name, sub_q in _sub_queries_raw.items():
                if mod_name in VALID_MODULES and isinstance(sub_q, str) and sub_q.strip():
                    sub_queries[mod_name] = sub_q.strip()

        # Defensive fallback: secondary ada tapi sub_queries kosong/incomplete
        if secondary and not sub_queries:
            logger.warning(
                "[CognitiveRouter] secondary=%s tapi sub_queries kosong → fallback ke query asli",
                secondary,
            )
        elif secondary and query:
            if module not in sub_queries:
                sub_queries[module] = query
            for sec in secondary:
                if sec not in sub_queries:
                    logger.warning(
                        "[CognitiveRouter] missing sub_query for secondary=%s → fallback",
                        sec,
                    )
                    sub_queries[sec] = query

        # Parse dependencies
        _deps_raw = obj.get("dependencies") or {}
        dependencies: Dict[str, Dict[str, Any]] = {}
        if isinstance(_deps_raw, dict):
            for _dep_mod, _dep_cfg in _deps_raw.items():
                if _dep_mod in VALID_MODULES and isinstance(_dep_cfg, dict):
                    dependencies[_dep_mod] = _dep_cfg

        ent = obj.get("entities", {}) or {}
        wil = ent.get("wilayah", {}) or {}

        def _norm(v):
            if v in (None, "null", "", "None"):
                return None
            return str(v).strip() or None

        # TAMBAHAN: ambil akreditasi_list dari LLM
        _akr_list = ent.get("akreditasi_list", [])
        if isinstance(_akr_list, list):
            akreditasi_list = [str(x).upper() for x in _akr_list if x]
        else:
            akreditasi_list = []

        def _safe_bool_field(v) -> Optional[bool]:
            if v in (None, "null", "", "None"):
                return None
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.lower() == "true"
            return bool(v)

        return CognitiveIntent(
            module=module,
            secondary=secondary,
            scope=scope,
            sub_queries=sub_queries,
            jenjang=_norm(ent.get("jenjang")),
            jenjang_list=[
                j for j in (ent.get("jenjang_list") or [])
                if isinstance(j, str) and j.strip()
            ],
            status=_norm(ent.get("status")),
            akreditasi=_norm(ent.get("akreditasi")),
            akreditasi_mode=_norm(ent.get("akreditasi_mode")),
            akreditasi_list=akreditasi_list,
            nama_sekolah=_norm(ent.get("nama_sekolah")),
            npsn=_norm(ent.get("npsn")),
            wilayah=WilayahEntities(
                provinsi=_norm(wil.get("provinsi")),
                kabupaten=_norm(wil.get("kabupaten")),
                kecamatan=_norm(wil.get("kecamatan")),
                desa=_norm(wil.get("desa")),
                landmark=_norm(wil.get("landmark")),
            ),
            wilayah_bandingkan=_norm(ent.get("wilayah_bandingkan")),
            yayasan=_norm(ent.get("yayasan")),
            koordinat_user=bool(ent.get("koordinat_user", False)),
            radius_km=_safe_float(ent.get("radius_km")),
            metric_target=_norm(ent.get("metric_target")),
            keaktifan=_norm(ent.get("keaktifan")),
            wilayah_terpencil=_safe_bool_field(ent.get("wilayah_terpencil")),
            wilayah_perbatasan=_safe_bool_field(ent.get("wilayah_perbatasan")),
            qc_masalah_only=bool(ent.get("qc_masalah_only", False)),
            qc_field=_safe_int(ent.get("qc_field")),
            qc_min_masalah=_safe_int(ent.get("qc_min_masalah")),
            sort_by=_norm(ent.get("sort_by")),
            sort_order=_norm(ent.get("sort_order")),
            qc_persen_min=_safe_float(ent.get("qc_persen_min")),
            qc_persen_max=_safe_float(ent.get("qc_persen_max")),
            qc_field_list=[
                x for x in (ent.get("qc_field_list") or [])
                if isinstance(x, int) and 1 <= x <= 13
            ],
            qc_persen_query=bool(ent.get("qc_persen_query", False)),
            is_oot=bool(obj.get("is_oot", False)),
            oot_reason=_norm(obj.get("oot_reason")),
            dependencies=dependencies,
            raw_label=raw[:80],
        )
    except Exception as e:
        logger.warning("[CognitiveRouter] parse error: %s | raw=%r", e, raw[:60])
        return None
 
# PATCH: typo normalization untuk cache key
_TYPO_MAP = {
    r"\bbrapa\b":    "berapa",
    r"\bskla[h]?\b": "sekolah",
    r"\bseklah\b":   "sekolah",
    r"\bsekoah\b":   "sekolah",
    r"\bsekolha\b":  "sekolah",
    r"\bnegri\b":    "negeri",
    r"\bnegrei\b":   "negeri",
    r"\bjmlh\b":     "jumlah",
    r"\bjmlah\b":    "jumlah",
    r"\btampilan\b": "tampilkan",
    r"\bdafatra\b":  "daftar",
    r"\bdafrtar\b":  "daftar",
    r"\bprovinsi\b": "provinsi",  # sudah benar tapi include sebagai anchor
    r"\bpropinsi\b": "provinsi",
    r"\bpropinsi\b": "provinsi",
    r"\bkabupetan\b": "kabupaten",
    r"\bkabupetan\b": "kabupaten",
}

def _canonical_query(q: str) -> str:
    """Normalisasi typo umum untuk cache key — jangan ubah query asli ke LLM."""
    q = q.lower().strip()
    q = re.sub(r"\s+", " ", q)
    for pattern, replacement in _TYPO_MAP.items():
        q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
    return q


def _cache_key(q: str) -> str:
    h = hashlib.md5(_canonical_query(q).encode()).hexdigest()
    return f"cognitive_router:v5:{h}"
# ════════════════════════════════════════════════════════════════
# MAIN CLASSIFIER
# ═══════════════════════════════════════════════════════════════
 
async def classify_and_extract(
    query:     str,
    use_cache: bool  = True,
    timeout:   float = _LLM_TIMEOUT,
) -> CognitiveIntent:
    """
    Entry point utama.
    Returns CognitiveIntent yang berisi module + semua entitas terekstrak.
    Tidak perlu memanggil smart_query_parser lagi setelah ini.
    """
    ck = _cache_key(query)
 
    # Cache read
    if use_cache:
        try:
            cached = await _intent_cache.get(ck)
            if cached:
                cached_copy = dict(cached)
                wil_data = cached_copy.pop("wilayah", {})
                cached_copy.setdefault("sort_by", None)
                cached_copy.setdefault("sort_order", None)
                cached_copy.setdefault("qc_persen_min", None)
                cached_copy.setdefault("qc_persen_max", None)
                cached_copy.setdefault("qc_field_list", [])
                cached_copy.setdefault("qc_persen_query", False)
                intent = CognitiveIntent(**cached_copy)
                intent.wilayah = WilayahEntities(**wil_data)
                return intent
        except Exception:
            pass
 
    # LLM call
    intent: Optional[CognitiveIntent] = None
    try:
        prompt = _SYSTEM_PROMPT + "\n\nQuery: " + query
        raw = await asyncio.wait_for(
            call_deepseek(prompt, max_tokens=500, temperature=0.0),
            timeout,
        )
        intent = _parse_llm_output(raw, query=query)
        if intent is not None:
            logger.info(
                "[CognitiveRouter] Parsed intent: module=%s secondary=%s scope=%s "
                "wilayah={prov=%s, kab=%s, kec=%s, desa=%s} "
                "jenjang=%s status=%s sub_queries_keys=%s",
                intent.module,
                intent.secondary,
                intent.scope,
                intent.wilayah.provinsi,
                intent.wilayah.kabupaten,
                intent.wilayah.kecamatan,
                intent.wilayah.desa,
                intent.jenjang,
                intent.status,
                list(intent.sub_queries.keys()),
            )
    except Exception as e:
        logger.warning(
            "[CognitiveRouter] LLM call failed: %s: %s",
            type(e).__name__, e or "(no message)",
        )
 
    # Keyword fallback
    if intent is None:
        logger.info("[CognitiveRouter] Using keyword fallback for: %r", query[:60])
        intent = _keyword_fallback(query)

    # ── Location resolution: normalise raw LLM names → canonical ES values ──
    try:
        from app.services.core.location_resolver import resolve_wilayah
        resolved = resolve_wilayah(intent.wilayah)
        intent.wilayah = WilayahEntities(
            provinsi=resolved.provinsi,
            kabupaten=resolved.kabupaten,
            kecamatan=resolved.kecamatan,
            desa=resolved.desa,
            landmark=intent.wilayah.landmark,  # landmark not resolved here
            ambiguous=resolved.ambiguous,
            candidates=list(resolved.candidates) if resolved.candidates else [],
        )
        if resolved.method in ("fuzzy", "unresolved") or resolved.ambiguous:
            logger.warning(
                "[LocationResolver] method=%s confidence=%.2f ambiguous=%s "
                "candidates=%s | wilayah=%s",
                resolved.method, resolved.confidence, resolved.ambiguous,
                resolved.candidates[:3], intent.wilayah,
            )
    except Exception as _lr_exc:
        logger.warning("[LocationResolver] skipped: %s", _lr_exc)

    # Cache write
    if use_cache:
        try:
            cache_data = {**intent.__dict__}
            cache_data["wilayah"] = intent.wilayah.__dict__
            await _intent_cache.set(ck, cache_data, ttl_l1=300, ttl_l2=1800)
        except Exception:
            pass
 
    return intent
 
 
async def classify_intent(
    query: str,
    use_cache: bool = True,
    timeout: float = _LLM_TIMEOUT,
) -> "CognitiveIntent":
    """
    Public alias untuk classify_and_extract.
    Nama ini digunakan oleh rag_server.py agar kompatibel
    dengan bekas llm_intent_classifier.
    """
    return await classify_and_extract(query, use_cache=use_cache, timeout=timeout)
 
 
def is_education_query(query: str) -> bool:
    """
    Shim pengganti utils.domain_filter.is_education_query.
    Menggunakan rule-based classifier — sinkron, tanpa LLM.
    Mengembalikan True jika query terdeteksi sebagai SCHOOL atau AMBIGUOUS.
    """
    from app.services.core.intent_classifier import (
        classify_intent_rule as _rule_classify,
        IntentType,
    )
    result = _rule_classify(query)
    return result != IntentType.OUT_OF_CONTEXT
