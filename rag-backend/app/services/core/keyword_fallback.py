"""
Keyword-based fallback classifier untuk cognitive_router.
Digunakan saat LLM tidak tersedia.
Dipindah dari cognitive_router.py (P17-B refactor).
"""
import re
import logging
from typing import Any, Dict, Optional

from app.services.core.intent_models import (
    CognitiveIntent, WilayahEntities, VALID_MODULES
)

logger = logging.getLogger(__name__)

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
    q      = query.lower().rstrip("?!.,;:")   # trailing punctuation breaks scope regex
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
    ) or re.search(r'\bpaling\s+banyak\b', q) or re.search(r'\bbanyak\s+sekolah\b', q) \
      or any(w in q for w in ("terbanyak", "terbesar", "tertinggi", "terendah", "terkecil",
                               "ranking", "peringkat")):
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
    # Prevent "provinsi dengan / kabupaten mana" from triggering scope (Fix 4).
    _SCOPE_STOP = frozenset({
        "dengan", "mana", "yang", "apa", "ini", "itu", "saja",
        "paling", "dan", "atau", "untuk", "dari", "ke", "berapa",
        "terbanyak", "terbesar", "tertinggi", "terendah", "terkecil",
    })

    def _level_is_filter(level_words):
        """True iff the word right after any level keyword is a real name (not a stopword)."""
        for lw in level_words:
            m = _re.search(r'\b' + _re.escape(lw) + r'\s+(\w+)', q)
            if m and m.group(1).lower() not in _SCOPE_STOP:
                return True
        return False

    if any(_re.search(p, q) for p in _DESA_PATTERNS):
        _scope = "desa"
    elif any(_re.search(p, q) for p in _KEC_PATTERNS) and _level_is_filter(["kecamatan", "kec."]):
        _scope = "kecamatan"
    elif any(_re.search(p, q) for p in _KAB_PATTERNS) and _level_is_filter(["kabupaten", "kab.", "kota"]):
        _scope = "kabupaten"
    elif any(_re.search(p, q) for p in _PROV_PATTERNS) and _level_is_filter(["provinsi", "prov."]):
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
    # WILAYAH EXTRACTION — parent-scope filter for ranking queries
    # "10 kabupaten dengan SMA terbanyak di jawa timur" → provinsi=jawa timur
    # Runs after scope so we know the ranking axis level (Fix 2).
    # ═══════════════════════════════════════════════════════════════
    _wilayah_from_ranking = None
    _RANKING_WORDS = ("terbanyak", "terbesar", "tertinggi", "terendah", "terkecil",
                      "ranking", "peringkat")
    if _scope in ("kabupaten", "kecamatan", "desa") and any(w in q for w in _RANKING_WORDS):
        _tail_di = _re.search(r'\bdi\s+([a-z][a-z\s]+?)\s*$', q)
        if _tail_di:
            _parent_raw = _tail_di.group(1).strip()
            _parent_loc = _WILAYAH_ALIAS.get(_parent_raw, _parent_raw)
            if _scope == "kabupaten":
                _wilayah_from_ranking = WilayahEntities(provinsi=_parent_loc)
            else:
                _wilayah_from_ranking = WilayahEntities(kabupaten=_parent_loc)

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

        _rank_level = "provinsi"  # default
        if any(w in q for w in ("kabupaten", "kab.", "kota")) and \
           not any(w in q for w in ("provinsi", "prov.")):
            _rank_level = "kabupaten"
        elif any(w in q for w in ("kecamatan", "kec.")):
            _rank_level = "kecamatan"

        _inject_map: Dict[str, str] = {_rank_level: "top_location"}
        if _jenjang_single is not None:
            _inject_map["jenjang"] = "top_jenjang"

        dependencies = {
            "list": {
                "depends_on": "aggregation",
                "inject_from_result": _inject_map,
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
        wilayah=_wilayah_from_ranking if _wilayah_from_ranking is not None else WilayahEntities(),
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
