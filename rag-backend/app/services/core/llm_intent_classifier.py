"""
services/core/llm_intent_classifier.py
 
Classifier intent untuk sistem RAG multi-modul Kemendikdasmen.
v3 — multi-label output (primary + secondary + filters)
"""
 
import re
import json
import hashlib
import logging
import asyncio
 
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from app.services.core.multi_intent_orchestrator import IntentResult

from app.utils.cache import get_cache, set_cache

logger = logging.getLogger(__name__)
 
 
# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL SETS
# ══════════════════════════════════════════════════════════════════════════════
 
# ── OOC: Topik yang JELAS di luar domain pendidikan ──────────────────────────
_OOC_STRONG = {
    # Politik & Pemerintahan
    "presiden", "gubernur", "bupati", "walikota", "pilpres", "pilkada",
    # Ekonomi & Finansial
    "saham", "ihsg", "bitcoin", "crypto", "forex", "investasi", "reksadana",
    # Hiburan & Lifestyle
    "film", "netflix", "spotify", "resep", "masakan", "traveling", "hotel",
    # Berita Umum
    "cuaca", "gempa", "tsunami", "covid", "vaksin", "dokter", "rumah sakit",
    # Internasional (bukan konteks pendidikan Indonesia)
    "israel", "palestina", "korea", "jepang", "inggris", "amerika",
}
 
# ── SCHOOL SIGNALS: Kata / token yang menandai domain pendidikan ──────────────
_SCHOOL_SIGNALS = {
    # Jenjang umum
    "sekolah", "pendidikan", "siswa", "guru", "kelas", "kurikulum",
    # Jenjang spesifik (standalone token)
    "sd", "smp", "sma", "smk", "mi", "mts", "ma", "tk", "paud", "slb",
    # Nomenklatur resmi (standalone token)
    "sman", "smpn", "sdn", "smkn", "man", "mtsn", "min",
    # Metadata sekolah
    "npsn", "akreditasi", "nilai", "ujian", "un", "us", "ppdb", "zonasi",
    # Tenaga pendidik
    "kepala sekolah", "honorer",
}
 
# ── GEO signals ───────────────────────────────────────────────────────────────
_GEO_SIGNALS = {
    "terdekat", "radius", "dekat sini", "dekat dari",
    "km dari", "lokasi terdekat", "sekitar sini",
}
 
# ── AGG signals (termasuk typo umum) ─────────────────────────────────────────
_AGG_SIGNALS = {
    "berapa", "jumlah", "jmlh", "jml", "total", "banyak",
    "terbanyak", "rata-rata", "hitung", "count",
}
 
# ── COMPARISON: KUAT (tidak ambigu dengan AGG) ───────────────────────────────
_STRONG_COMP = {
    "bandingkan", "vs", "versus", "dibanding", "perbandingan",
    "lebih banyak", "mana yang lebih", "mana lebih",
}
 
# ── LIST signals (termasuk "sebutkan" untuk TC_AGG_004) ──────────────────────
_LIST_SIGNALS = {
    "daftar", "list", "tampilkan", "carikan", "cari",
    "rekomendasikan", "apa saja", "sebutkan", "tunjukkan",
}
 
# ── PROFILE signals ───────────────────────────────────────────────────────────
_PROFILE_SIGNALS = {
    "profil", "info", "detail", "alamat", "telepon", "website",
    "terakreditasi", "npsn", "informasi sekolah",
}

# Precompile word-boundary patterns untuk profile signals.
# Ini WAJIB — substring check naif seperti `"alamat" in "ada berapa ... alamatnya"`
# akan memberikan false positive dan salah routing ke profile.
_PROFILE_SIGNAL_PATTERNS = [
    re.compile(r'\b' + re.escape(w) + r'\b', re.I)
    for w in _PROFILE_SIGNALS
]
 
 
# ══════════════════════════════════════════════════════════════════════════════
# NAMED SCHOOL ABBREVIATION MAP  (SMAN → SMA + NEGERI, dll.)
# ══════════════════════════════════════════════════════════════════════════════
 
_NAMED_SCHOOL_MAP: Dict[str, tuple] = {
    "sman":  ("SMA", "NEGERI"),
    "smpn":  ("SMP", "NEGERI"),
    "sdn":   ("SD",  "NEGERI"),
    "smkn":  ("SMK", "NEGERI"),
    "man":   ("MA",  "NEGERI"),
    "mtsn":  ("MTs", "NEGERI"),
    "min":   ("MI",  "NEGERI"),
    "tkn":   ("TK",  "NEGERI"),
}
 
# Pattern untuk deteksi singkatan sekolah negeri (word-boundary safe)
_NAMED_SCHOOL_RE = re.compile(
    r'\b(sman|smpn|sdn|smkn|man|mtsn|min|tkn)\b', re.I
)
 
# Pattern untuk deteksi sekolah SPESIFIK (nama + nomor): "SMAN 3", "SDN 02"
# Logika: (sd|smp|sma|...)  + opsional "n"  + spasi + angka
_SPECIFIC_SCHOOL_RE = re.compile(
    r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk)n?\s*\d+', re.I
)
 
 
# ══════════════════════════════════════════════════════════════════════════════
# REGEX FILTER EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════
 
_AKRED_RE   = re.compile(r'\bakreditasi\s+([abc])\b', re.I)
_JENJANG_RE = re.compile(r'\b(sd|smp|sma|smk|mi|mts|ma|tk|paud)\b', re.I)
_STATUS_RE  = re.compile(r'\b(negeri|swasta)\b', re.I)
_PROFILE_WORD_RE = re.compile(
    r'\b(profil|profile|alamat)\b'
    r'|\bterakreditasi\b(?!\s+[abc]\b)',   # terakreditasi TANPA grade
    re.IGNORECASE,
)
 
def _extract_filters(q: str) -> Dict[str, Any]:
    """
    Ekstrak filter akreditasi, jenjang, dan status dari query.
 
    Urutan prioritas jenjang/status:
    1. Named abbreviation (SMAN → SMA + NEGERI) — paling presisi
    2. Regex standalone jenjang + regex status — fallback
    """
    filters: Dict[str, Any] = {
        "akreditasi": None,
        "jenjang":    None,
        "status":     None,
    }
 
    # Akreditasi: "akreditasi A/B/C"
    m = _AKRED_RE.search(q)
    if m:
        filters["akreditasi"] = m.group(1).upper()
 
    # Jenjang + Status dari nama resmi (SMAN, SDN, SMKN, dll.)
    m = _NAMED_SCHOOL_RE.search(q)
    if m:
        abbr = m.group(1).lower()
        jenjang, status = _NAMED_SCHOOL_MAP[abbr]
        filters["jenjang"] = jenjang
        filters["status"]  = status
    else:
        # Fallback: standalone jenjang token
        m = _JENJANG_RE.search(q)
        if m:
            filters["jenjang"] = m.group(1).upper()
 
        # Fallback: "negeri" / "swasta"
        m = _STATUS_RE.search(q)
        if m:
            filters["status"] = m.group(1).upper()
 
    return filters
 
 
# ══════════════════════════════════════════════════════════════════════════════
# ENUM LAMA (WAJIB DIPERTAHANKAN — backward-compat)
# ══════════════════════════════════════════════════════════════════════════════
 
class QueryRoute(str, Enum):
    AGGREGATION    = "aggregation"
    COMPARISON     = "comparison"
    PROFILE        = "profile"
    LIST           = "list"
    GEO            = "geo"
    OUT_OF_CONTEXT = "out_of_context"
 
 
# ══════════════════════════════════════════════════════════════════════════════
# RESULT OBJECT
# ══════════════════════════════════════════════════════════════════════════════
 
@dataclass
class IntentResult:
    primary:   str
    secondary: List[str]            = field(default_factory=list)
    filters:   Dict[str, Any]       = field(default_factory=dict)
    raw_label: Optional[str]        = None
 
 
VALID_MODULES = {r.value for r in QueryRoute}
_CACHE_TTL    = 86_400
 

# ============================================================================
# PATCH: GEO user-position detection improvement
# Bedakan:
#   "terdekat dari sini"   → geo (butuh GPS user)
#   "terdekat dari Monas"  → list + geo secondary (landmark statis)
# ============================================================================

_GEO_USER_SIGNALS = {
    "dari sini", "dari posisi", "posisi saya", "posisi ku",
    "lokasi saya", "lokasi ku", "koordinat", "gps", "dekat sini",
}

_GEO_RADIUS_RE = re.compile(r'\bradius\s+\d+\s*km\b', re.IGNORECASE)

_GEO_STRONG_SIGNALS = {
    "dekat sini",
    "terdekat dari sini",
    "sekitar sini"
}


def _is_geo_user_based(q: str) -> bool:
    """
    True  → query butuh posisi user (GPS)
    False → hanya filter lokasi berdasarkan nama tempat
    """

    # sinyal eksplisit posisi user
    if any(sig in q for sig in _GEO_USER_SIGNALS):
        return True

    # radius tanpa referensi tempat → geo user based
    if _GEO_RADIUS_RE.search(q):

        # contoh:
        # "radius 2 km dari Monas" → list
        if re.search(r'\bradius\b.{1,20}\bdari\s+\w', q):
            return False

        return True

    # kata "terdekat"
    if "terdekat" in q:

        m = re.search(r'\bterdekat\s+dari\s+(\w+)', q)

        if m:

            after_dari = m.group(1).lower()

            # contoh:
            # "terdekat dari sini" → geo
            if after_dari in {
                "sini",
                "saya",
                "aku",
                "ku",
                "rumah",
                "posisi"
            }:
                return True

            # contoh:
            # "terdekat dari Monas" → list
            return False

        # hanya kata "terdekat" tanpa referensi tempat
        # biasanya user maksud lokasi dirinya
        return True

    return False

# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD FALLBACK  (offline, tanpa LLM)
# ══════════════════════════════════════════════════════════════════════════════
 
def _keyword_fallback(query: str):   # return type: IntentResult
    """
    Keyword fallback — urutan prioritas yang sudah diperbaiki:
    1. OOC gatekeeper
    2. School vs school (dua nama spesifik + vs) → profile
    3. COMPARISON (bandingkan/vs/lebih banyak) — dicek SEBELUM aggregation
    4. Aggregation (berapa/jumlah/total)
    5. Geo berbasis posisi user
    6. List (daftar/tampilkan/apa saja) + geo secondary jika ada landmark ref
    7. Profile signal word (alamat/profil/terakreditasi) + school context
    8. Specific school RE (SMAN 3, SDN 02)
    9. NPSN
    10. Default list jika ada school signal
    11. OOC total fallback
    """
 
    if not query or not query.strip():
        return IntentResult(primary="out_of_context")
 
    q      = query.lower()
    tokens = set(q.split())
    filters = _extract_filters(q)   # fungsi sudah ada di file, tidak berubah
 
    # ── 1. OOC gatekeeper ────────────────────────────────────────────────────
    has_ooc = any(w in q for w in _OOC_STRONG)
    has_school = (
        any(w in tokens for w in _SCHOOL_SIGNALS)
        or bool(re.search(r'\b(?:sd|smp|sma|smk|mi|mts|ma|tk)n?\b', q))
    )
    if has_ooc and not has_school:
        return IntentResult(primary="out_of_context", filters=filters)
 
    # ── 2. School vs school (dua nama spesifik + vs) → profile ───────────────
    specific_schools = _SPECIFIC_SCHOOL_RE.findall(q)
    if len(specific_schools) >= 2 and any(w in q for w in _STRONG_COMP):
        return IntentResult(primary="profile", filters=filters)
 
    # ── 3. COMPARISON — dicek SEBELUM aggregation ─────────────────────────────
    # Kunci: "bandingkan jumlah...", "vs...lebih banyak", "mana yang lebih banyak"
    # memiliki KEDUA sinyal. Comparison lebih spesifik → menang.
    if any(w in q for w in _STRONG_COMP):
        secondary = []
        if any(w in q for w in _LIST_SIGNALS):
            secondary.append("list")
        return IntentResult(primary="comparison", secondary=secondary, filters=filters)
 
    # ── 4. Aggregation ───────────────────────────────────────────────────────
    if any(w in q for w in _AGG_SIGNALS):
        secondary = []
        if any(w in q for w in _LIST_SIGNALS):
            secondary.append("list")
        elif _is_geo_user_based(q):
            secondary.append("geo")
        return IntentResult(primary="aggregation", secondary=secondary, filters=filters)
 
    # ── 5. Geo berbasis posisi user ───────────────────────────────────────────
    if _is_geo_user_based(q):
        secondary = []
        if any(w in q for w in _LIST_SIGNALS):
            secondary.append("list")
        return IntentResult(primary="geo", secondary=secondary, filters=filters)
 
    # ── 6. List (+ geo secondary jika ada referensi landmark) ────────────────
    if any(w in q for w in _LIST_SIGNALS):
        secondary = []
        if "terdekat" in q or "sekitar" in q:
            secondary.append("geo")
        return IntentResult(primary="list", secondary=secondary, filters=filters)
 
    # ── 7. Profile signal word + school context ───────────────────────────────
    # Menangkap: "alamat SMK Pelita Harapan", "apakah SMK X terakreditasi?"
    # tanpa perlu nomor sekolah.
    # _PROFILE_WORD_RE sudah excludes "terakreditasi A" (dengan grade) → aman.
    if _PROFILE_WORD_RE.search(q) and has_school:
        return IntentResult(primary="profile", filters=filters)
 
    # ── 8. Specific school name + nomor → profile ────────────────────────────
    if _SPECIFIC_SCHOOL_RE.search(q):
        return IntentResult(primary="profile", filters=filters)
 
    # ── 9. NPSN → profile ────────────────────────────────────────────────────
    if re.search(r'\b\d{8}\b', q):
        return IntentResult(primary="profile", filters=filters)
 
    # ── 10. Default: ada school signal → list ────────────────────────────────
    if has_school:
        return IntentResult(primary="list", filters=filters)
 
    # ── 11. Tidak ada signal apapun → OOC ────────────────────────────────────
    return IntentResult(primary="out_of_context", filters=filters)
 
 
# ══════════════════════════════════════════════════════════════════════════════
# PROMPT
# ══════════════════════════════════════════════════════════════════════════════
 
_SYSTEM_PROMPT = """\
Kamu adalah router intent untuk sistem informasi satuan pendidikan Indonesia (Kemendikdasmen).
Sistem ini mengelola data: sekolah (SD/SMP/SMA/SMK/MI/MTs/MA/TK/PAUD/SLB),
guru & tenaga pendidik (PTK), siswa, akreditasi, dan data operasional pendidikan.
 
Tugasmu: klasifikasikan query pengguna ke format JSON berikut.
 
Format output (WAJIB valid JSON, tidak ada teks lain):
{"primary": "<label>", "secondary": ["<label>", ...], "filters": {"akreditasi": null, "jenjang": null, "status": null}}
 
Label yang tersedia:
  aggregation  → menghitung jumlah/total/ranking sekolah, guru, atau siswa di suatu wilayah
  comparison   → membandingkan antar WILAYAH atau antar STATUS sekolah (negeri vs swasta)
  profile      → detail/profil/alamat SATU sekolah SPESIFIK (ada nama atau NPSN)
  list         → menampilkan daftar sekolah berdasarkan area/filter
  geo          → mencari sekolah terdekat dari POSISI USER saat ini (butuh koordinat GPS)
  out_of_context → pertanyaan di luar domain pendidikan Indonesia
 
ATURAN DOMAIN — apa yang TERMASUK domain sistem ini:
  - Data sekolah: jumlah, profil, daftar, lokasi, akreditasi
  - Data guru & tenaga pendidik (PTK): honorer, PNS, sertifikasi
  - Data siswa & peserta didik
  → Semua ini WAJIB diklasifikasikan ke salah satu label di atas, BUKAN out_of_context
 
ATURAN KRITIS — comparison vs aggregation (PENTING):
  - Jika ada kata perbandingan (bandingkan/vs/versus/lebih banyak/mana yang lebih) → SELALU comparison
  - Berlaku MESKIPUN ada kata hitungan (jumlah/berapa/banyak) dalam query yang sama
  - Contoh: "bandingkan jumlah..." → comparison (bukan aggregation)
  - Contoh: "mana yang lebih banyak... A atau B?" → comparison (bukan aggregation)
 
ATURAN KRITIS — profile vs comparison:
  - "SMAN 1 X vs SMAN 2 X" (DUA nama sekolah spesifik + vs) → profile (school_vs_school)
  - "sekolah di Bandung vs sekolah di Bogor" (dua WILAYAH) → comparison
 
ATURAN KRITIS — profile tanpa nomor:
  - "alamat [nama sekolah]" → profile (meski tidak ada nomor)
  - "apakah [nama sekolah] terakreditasi?" → profile (tanya akreditasi sekolah spesifik)
  - "[nama sekolah] di mana letaknya?" → profile
 
ATURAN KRITIS — profile vs geo:
  - "di mana letak [nama sekolah spesifik]" → profile
  - "alamat [nama sekolah spesifik]" → profile
  - "sekolah terdekat dari SINI/posisiku/koordinat" → geo
 
ATURAN KRITIS — geo vs list:
  - geo HANYA jika user mencari dari POSISINYA SENDIRI ("dari sini", "radius X km", koordinat GPS)
  - "sekolah di sekitar [landmark]" atau "daftar terdekat dari [tempat]" → list
 
ATURAN QUERY CAMPURAN — jika ada OOC + pendidikan:
  → ABAIKAN bagian OOC, klasifikasikan berdasarkan bagian pendidikan
 
Aturan secondary:
  - Isi secondary HANYA jika query EKSPLISIT meminta dua hal ("tampilkan juga", "sebutkan juga")
  - Maksimal 2 label. Jangan isi jika tidak diminta eksplisit.
 
Aturan filters:
  - akreditasi: "A", "B", "C", atau null
  - jenjang: "SD","SMP","SMA","SMK","MI","MTs","MA","TK" atau null
  - status: "NEGERI", "SWASTA", atau null
 
Contoh:
Query: "berapa SMK akreditasi A di Bandung"
→ {"primary":"aggregation","secondary":[],"filters":{"akreditasi":"A","jenjang":"SMK","status":null}}
 
Query: "bandingkan jumlah SMK di Bandung dan Bogor, tampilkan juga daftarnya"
→ {"primary":"comparison","secondary":["list"],"filters":{"jenjang":"SMK","akreditasi":null,"status":null}}
 
Query: "bandingkan jumlah sekolah di Cisauk dan Serpong"
→ {"primary":"comparison","secondary":[],"filters":{"akreditasi":null,"jenjang":null,"status":null}}
 
Query: "mana yang lebih banyak sekolahnya, Jakarta Selatan atau Jakarta Timur?"
→ {"primary":"comparison","secondary":[],"filters":{"akreditasi":null,"jenjang":null,"status":null}}
 
Query: "SMK negeri vs swasta di mana lebih banyak?"
→ {"primary":"comparison","secondary":[],"filters":{"jenjang":"SMK","akreditasi":null,"status":null}}
 
Query: "profil SMAN 1 Bandung"
→ {"primary":"profile","secondary":[],"filters":{"jenjang":"SMA","status":"NEGERI","akreditasi":null}}
 
Query: "di mana letak SDN 02 Parung?"
→ {"primary":"profile","secondary":[],"filters":{"jenjang":"SD","status":"NEGERI","akreditasi":null}}
 
Query: "alamat SMK Pelita Harapan Tangerang"
→ {"primary":"profile","secondary":[],"filters":{"jenjang":"SMK","akreditasi":null,"status":null}}
 
Query: "apakah SMK Pelita Harapan terakreditasi?"
→ {"primary":"profile","secondary":[],"filters":{"jenjang":"SMK","akreditasi":null,"status":null}}
 
Query: "SMAN 1 Depok vs SMAN 2 Depok"
→ {"primary":"profile","secondary":[],"filters":{"jenjang":"SMA","status":"NEGERI","akreditasi":null}}
 
Query: "daftar SMA terdekat dari Monas"
→ {"primary":"list","secondary":["geo"],"filters":{"jenjang":"SMA","akreditasi":null,"status":null}}
 
Query: "sekolah dalam radius 2 km dari posisi saya"
→ {"primary":"geo","secondary":[],"filters":{"akreditasi":null,"jenjang":null,"status":null}}
 
Query: "cuaca Jakarta besok dan berapa sekolah di Jakarta?"
→ {"primary":"aggregation","secondary":[],"filters":{"akreditasi":null,"jenjang":null,"status":null}}
 
Query: "berapa guru honorer di Jawa Barat"
→ {"primary":"aggregation","secondary":[],"filters":{"akreditasi":null,"jenjang":null,"status":null}}
 
Query: "harga saham hari ini"
→ {"primary":"out_of_context","secondary":[],"filters":{"akreditasi":null,"jenjang":null,"status":null}}
 
Balas HANYA dengan JSON valid. Tanpa markdown, tanpa penjelasan, tanpa ```json.
"""
 
_USER_TEMPLATE = "Query: {query}"
 
 
# ══════════════════════════════════════════════════════════════════════════════
# PARSE OUTPUT LLM
# ══════════════════════════════════════════════════════════════════════════════
 
def _parse_llm_output(raw: str) -> Optional[IntentResult]:
    if not raw:
        return None
    try:
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw, flags=re.I).strip()
        match   = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        obj = json.loads(match.group(0))
 
        primary = obj.get("primary", "").lower().strip()
        if primary not in VALID_MODULES:
            return None
 
        secondary = [
            s.lower() for s in (obj.get("secondary") or [])
            if isinstance(s, str) and s.lower() in VALID_MODULES
        ]
 
        raw_filters = obj.get("filters") or {}
        filters = {
            "akreditasi": raw_filters.get("akreditasi"),
            "jenjang":    raw_filters.get("jenjang"),
            "status":     raw_filters.get("status"),
        }
        # Normalisasi: "null" / "" → None, valid value → UPPERCASE
        filters = {
            k: (None if v in (None, "null", "") else str(v).upper())
            for k, v in filters.items()
        }
 
        return IntentResult(
            primary=primary, secondary=secondary,
            filters=filters, raw_label=raw[:80]
        )
    except Exception as e:
        logger.warning("[Intent] parse error: %s | raw=%r", e, raw[:60])
        return None
 
 
# ══════════════════════════════════════════════════════════════════════════════
# CACHE KEY
# ══════════════════════════════════════════════════════════════════════════════
 
def _cache_key(q: str) -> str:
    h = hashlib.md5(q.lower().encode()).hexdigest()
    return f"intent:v3:{h}"
 
 
# ══════════════════════════════════════════════════════════════════════════════
# CLASSIFIER UTAMA
# ══════════════════════════════════════════════════════════════════════════════
 
async def classify_intent(
    query:     str,
    use_cache: bool  = True,
    timeout:   float = 4.0,
) -> IntentResult:
    """
    Klasifikasi intent dengan LLM (DeepSeek) + keyword fallback.
 
    Args:
        query:     teks pertanyaan pengguna
        use_cache: True → cek/tulis Redis cache (default production)
                   False → bypass cache, selalu hit LLM / fallback
        timeout:   batas waktu panggil LLM (detik)
    """
    ck = _cache_key(query)
 
    # ── Cache read ────────────────────────────────────────────────────────────
    if use_cache:
        try:
            cached = await get_cache(ck)
            if cached:
                return IntentResult(**cached)
        except Exception:
            pass
 
    # ── LLM call ──────────────────────────────────────────────────────────────
    intent: Optional[IntentResult] = None
    try:
        from app.providers.deepseek import call_deepseek
 
        prompt = _SYSTEM_PROMPT + "\n\n" + _USER_TEMPLATE.format(query=query)
        raw    = await asyncio.wait_for(
            call_deepseek(prompt, max_tokens=80),
            timeout,
        )
        intent = _parse_llm_output(raw)
    except Exception:
        pass
 
    # ── Keyword fallback jika LLM gagal ──────────────────────────────────────
    if intent is None:
        intent = _keyword_fallback(query)
 
    # ── Cache write ───────────────────────────────────────────────────────────
    if use_cache:
        try:
            await set_cache(ck, intent.__dict__, _CACHE_TTL)
        except Exception:
            pass
 
    return intent
 
 
# ══════════════════════════════════════════════════════════════════════════════
# WRAPPER LAMA (backward-compat — tidak merusak kode existing)
# ══════════════════════════════════════════════════════════════════════════════
 
async def classify_query_intent(query: str) -> QueryRoute:
    intent = await classify_intent(query)
    try:
        return QueryRoute(intent.primary)
    except Exception:
        return QueryRoute.OUT_OF_CONTEXT