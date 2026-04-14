# services/core/intent_classifier.py
"""
IntentClassifier — guard layer yang berjalan SEBELUM routing ke Elasticsearch.
 
Mendeteksi apakah query berkaitan dengan konteks sekolah/pendidikan atau
berada di luar cakupan sistem, sehingga query OOC bisa dijawab langsung
oleh LLM tanpa menyentuh ES sama sekali.
 
Cara pakai (di rag_server.py atau router.py, paling awal sebelum routing):
 
    from services.core.intent_classifier import classify_intent, IntentType
 
    intent = classify_intent(query)
    if intent == IntentType.OUT_OF_CONTEXT:
        answer = await narrate_out_of_context(query)
        return build_response(answer, provider="SYSTEM_OUT_OF_CONTEXT")
    # ... lanjut ke routing ES seperti biasa
"""
 
import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List
 
__all__ = ["classify_intent", "classify_intent_rule", "IntentResult"]
 
 
@dataclass
class IntentResult:
    """
    Output dari classifier v2.
    Menggantikan QueryRoute tunggal dengan struktur multi-label.
    """
    primary: str                        # "aggregation" | "comparison" | "profile" | "list" | "geo"
    secondary: List[str] = field(default_factory=list)   # modul pendukung
    filters: Dict[str, Any] = field(default_factory=dict) # {"akreditasi": "A", "jenjang": "SMK", ...}
    raw_label: str = ""                 # label mentah dari LLM (untuk debug)
logger = logging.getLogger(__name__)
 
 
class IntentType(str, Enum):
    SCHOOL        = "school"          # → teruskan ke router ES
    OUT_OF_CONTEXT = "out_of_context" # → jawab langsung via LLM + disclaimer
    AMBIGUOUS     = "ambiguous"       # → teruskan ke router ES (biarkan ES handle)
 
 
# ─────────────────────────────────────────────────────────────────────────────
# KEYWORD LISTS
# ─────────────────────────────────────────────────────────────────────────────
 
# Kata/frasa yang kuat mengindikasikan konteks sekolah / pendidikan
# Frasa multi-kata diberi bobot 2x oleh scorer
SCHOOL_KEYWORDS: list[str] = [
    # Intent query
    "sekolah", "daftar sekolah", "jumlah sekolah", "profil sekolah",
    "list sekolah", "tampilkan sekolah",     # Fix: 'list' sebagai alias 'daftar'
    "bandingkan", "perbandingan", "vs", "versus",
    # Jenjang
    "sd", "mi", "smp", "mts", "sma", "smk", "ma", "tk", "paud",
    "kb", "tpa", "slb",
    "sd negeri", "smp negeri", "sma negeri", "smk negeri",
    "madrasah", "ibtidaiyah", "tsanawiyah", "aliyah",
    "pesantren", "pondok",
    "universitas", "institut", "politeknik", "akademi", "kampus",
    # Jenjang non-common yang ada di data ES
    "ra", "sps", "pkbm", "skb", "kursus",
    "pondok pesantren",
    "sekolah tinggi",
    # Identitas sekolah
    "npsn", "akreditasi",
    # Pelaku pendidikan
    "siswa", "murid", "peserta didik",
    "guru", "tenaga pendidik", "ptk", "kepala sekolah",
    "pns", "honorer", "sertifikasi guru",
    # Sarana
    "ruang kelas", "laboratorium", "perpustakaan",
    # Singkatan nama sekolah
    "sman", "smpn", "sdn", "tkn", "smkn", "man", "mtsn", "min",
]
 
# Kata/frasa yang mengindikasikan query di LUAR konteks sekolah
OUT_OF_CONTEXT_KEYWORDS: list[str] = [
    # Politik & pemerintahan
    "presiden", "gubernur", "bupati", "walikota", "menteri",
    "dpr", "dprd", "mpr",
    "pilpres", "pilkada", "pemilu",
    "partai politik", "koalisi", "kabinet",
    "undang-undang", "peraturan pemerintah",
    # Ekonomi & keuangan
    "saham", "bursa efek", "ihsg", "kurs", "valuta asing",
    "inflasi", "deflasi", "resesi", "gdp", "pdb",
    "harga minyak", "harga emas", "harga bbm",
    "bank sentral", "bi rate", "suku bunga",
    "kripto", "bitcoin", "ethereum",
    # Berita & hiburan
    "berita terbaru", "breaking news",
    "film", "bioskop", "netflix",
    "lagu", "musik", "konser",
    "resep masak", "kuliner", "restoran",
    "liga", "pertandingan bola", "skor pertandingan",
    "sepakbola", "bulu tangkis", "tinju",
    # Teknologi umum
    "chatgpt", "openai", "gemini", "gpt-4",
    "iphone terbaru", "samsung terbaru",
    # Cuaca & bencana
    "cuaca hari ini", "prakiraan cuaca", "bmkg",
    "gempa bumi", "tsunami", "banjir bandang",
    # Negara & geografi dunia (nama negara tidak terkait Indonesia)
    "bendera", "ibu kota negara",
    "israel", "palestina", "rusia", "ukraina",
    "polandia", "jepang", "korea selatan", "korea utara",
    "inggris", "prancis", "jerman", "belanda",
    "australia", "kanada", "brazil", "india",
    "arab saudi", "turki", "iran", "irak",
    "penduduk dunia", "peta dunia",
    # Pertanian & komoditas
    "sawit", "kelapa sawit", "padi", "jagung", "kedelai",
    "kakao", "kopi", "tebu", "singkong",
    "hama tanaman", "pestisida", "pupuk", "panen",
    "ternak", "peternakan", "budidaya",
    # Kesehatan & penyakit
    "penyakit", "gejala penyakit", "gejala",
    "obat-obatan", "obat untuk", "minum obat",
    "rumah sakit", "dokter spesialis", "dokter",
    "virus corona", "covid", "diabetes", "kanker",
    "vaksin", "imunisasi", "demam berdarah",
    # Hukum & kriminal
    "pengadilan", "pidana", "tindak korupsi", "kriminal",
    # Frasa pertanyaan umum non-pendidikan
    "siapa presiden", "presiden indonesia", "presiden amerika",
    "presiden china", "perdana menteri", "raja", "ratu",
    "berapa umur", "tanggal lahir",
]
 
# Pola perbandingan implisit: "sekolah X dan Y" tanpa kata 'bandingkan'
# → sinyal ini perlu dikenali agar tidak jatuh ke profile handler
# Contoh: "sekolah parungpanjang dan parung", "sekolah di cibunar dengan lumpang"
_IMPLICIT_COMPARISON_RE = re.compile(
    # Negative lookahead: jangan match jika ada kata sifat/kualitas
    # sehingga "sekolah bagus dan murah di X" tidak salah → comparison
    r"\bsekolah\b"
    r"(?!.*\b(bagus|murah|favorit|terbaik|terdekat|unggulan|"
    r"ternama|populer|terkenal|berkualitas|rekomendasi)\b)"
    r"[\w\s]{2,40}\b(dan|dengan)\b[\w\s]{2,30}",
    re.IGNORECASE,
)
# Guard: jika semua token setelah "sekolah" adalah jenjang/kata filter, bukan lokasi
_JENJANG_ONLY_RE = re.compile(
    r"^(sekolah\s+)?(sd|smp|sma|smk|tk|mi|mts|ma|paud|kb|ra|sps|tpa)"
    r"(\s*[,/]\s*(sd|smp|sma|smk|tk|mi|mts|ma|paud|kb|ra|sps|tpa))*"
    r"(\s+(dan|atau|serta|dengan)\s+(sd|smp|sma|smk|tk|mi|mts|ma|paud|kb|ra|sps|tpa))*"
    r"\s+di\b",
    re.IGNORECASE,
)
 
# Pola regex: nama sekolah eksplisit seperti "SMAN 1", "SDN 02", "MAN 3"
# → jika match, langsung SCHOOL tanpa perlu scoring keyword
_SCHOOL_NAME_RE = re.compile(
    r"\b(sd|mi|smp|mts|sma|smk|ma|tk|paud|sman|smpn|sdn|tkn|smkn|man|mtsn|min)"
    r"\s*n?\s*\d",
    re.IGNORECASE,
)
 
# Pola definitional: sinyal kuat bahwa user menanyakan definisi/konsep
# bukan mencari sekolah. Contoh: "sawit itu apa", "polandia dimana",
# "penyakit sawit gila apa".
# CATATAN: hanya dipakai jika school_score == 0 (tidak ada sinyal sekolah)
_DEFINITIONAL_RE = re.compile(
    r"(?:"
    r"\b\w[\w\s]{1,40}\s+itu\s+apa\b"       # "sawit itu apa"
    r"|apa\s+itu\s+\w[\w\s]{1,40}"           # "apa itu sawit"
    r"|\b\w[\w\s]{1,40}\s+adalah\s+apa\b"   # "sawit adalah apa"
    r"|\b\w[\w\s]{1,30}\s+dimana\b"          # "polandia dimana"
    r"|dimana\s+\w[\w\s]{1,30}"              # "dimana polandia"
    r"|\bapa\s+yang\s+dimaksud\b"            # "apa yang dimaksud dengan X"
    r"|\bapa\s+fungsi\b"                     # "apa fungsi X"
    r"|\bsiapa\s+(?:itu\s+)?\w[\w\s]{1,30}" # "siapa itu X"
    r")",
    re.IGNORECASE,
)
 
# ── HARD OOT GUARD (lebih agresif, early detection) ──
_HARD_OOT_PATTERNS = re.compile(
    r'\b(harga|berapa ribu|burger|makanan|resep|presiden|gubernur|cuaca|saham|film|netflix)\b',
    re.I
)
 
_SCHOOL_SIGNALS_RE = re.compile(
    r'\b(sekolah|siswa|guru|sd|smp|sma|smk|mi|mts|ma|tk|paud|slb|npsn|akreditasi|ppdb|zonasi|spp|uang\s+sekolah|biaya\s+sekolah)\b',
    re.I
)
 
def is_clearly_oot(query: str) -> bool:
    has_oot = bool(_HARD_OOT_PATTERNS.search(query))
    has_edu = bool(_SCHOOL_SIGNALS_RE.search(query))
    return has_oot and not has_edu
 
# ─────────────────────────────────────────────────────────────────────────────
# CLASSIFIER
# ─────────────────────────────────────────────────────────────────────────────
 
def _normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", " ", text.lower()).strip()
 
 
def _score_keywords(q: str, keywords: list[str]) -> int:
    """
    Hitung skor keyword dengan word-boundary matching.
    Frasa multi-kata (mengandung spasi) diberi bobot 2x.
    Single-word keyword pendek (≤3 karakter) HARUS match whole-word
    untuk menghindari false positive dari substring.
    """
    score = 0
    for kw in keywords:
        weight = 2 if " " in kw else 1
        if " " in kw:
            # Frasa: cukup substring match (sudah cukup spesifik)
            if kw in q:
                score += weight
        else:
            # Kata tunggal: wajib word boundary
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, q):
                score += weight
    return score
 
 
def classify_intent_rule(query: str) -> IntentType:
    """
    Klasifikasikan intent query sebelum diteruskan ke router ES.
    Versi rule-based (sinkron, tanpa LLM).
 
    Returns:
        IntentType.SCHOOL          → teruskan ke routing ES seperti biasa
        IntentType.OUT_OF_CONTEXT  → jawab via narrate_out_of_context()
        IntentType.AMBIGUOUS       → teruskan ke routing ES (biarkan ES handle)
    """
    q = _normalize(query)
 
    # ── HARD OOT FAST PATH (paling awal, super cepat) ──
    if is_clearly_oot(q):
        logger.debug("[INTENT] hard OOT pattern match → OUT_OF_CONTEXT")
        return IntentType.OUT_OF_CONTEXT
    
    # ── Fast path: pola nama sekolah spesifik (sangat kuat) ─────────────────
    if _SCHOOL_NAME_RE.search(q):
        logger.debug("[INTENT] school-name pattern match → SCHOOL")
        return IntentType.SCHOOL
 
    # ── Fast path: perbandingan implisit "sekolah X dan Y" → SCHOOL ─────────
    if _IMPLICIT_COMPARISON_RE.search(q):
        # Guard: jika pola hanya jenjang+jenjang di satu lokasi, bukan perbandingan lokasi
        if not _JENJANG_ONLY_RE.search(q):
            logger.debug("[INTENT] implicit comparison pattern → SCHOOL")
            return IntentType.SCHOOL
 
    # ── Scoring keyword ──────────────────────────────────────────────────────
    school_score = _score_keywords(q, SCHOOL_KEYWORDS)
    ooc_score    = _score_keywords(q, OUT_OF_CONTEXT_KEYWORDS)
 
    # ── Pola definitional: "X itu apa", "X dimana", dst ─────────────────────
    # Hanya aktif jika tidak ada sinyal sekolah sama sekali
    is_definitional = school_score == 0 and bool(_DEFINITIONAL_RE.search(q))
 
    logger.debug(
        "[INTENT] query=%r | school=%d | ooc=%d | definitional=%s",
        query[:80], school_score, ooc_score, is_definitional,
    )
 
    # ── Keputusan ────────────────────────────────────────────────────────────
    # Guard: jika ooc_score sangat tinggi (≥3) dan satu-satunya sinyal sekolah
    # hanya dari kata tunggal pendek ambigu, jangan override OOT.
    # 'pns' dan 'honorer' sering muncul dalam konteks ketenagakerjaan umum,
    # 'kb' = keluarga berencana, bukan jenjang sekolah.
    _AMBIGUOUS_SHORT_SCHOOL = {"pns", "honorer", "kb", "sertifikasi"}
    if ooc_score >= 3 and school_score > 0:
        school_signals_found = [
            kw for kw in SCHOOL_KEYWORDS
            if (" " in kw and kw in q) or
               (" " not in kw and re.search(r"\b" + re.escape(kw) + r"\b", q))
            if kw.lower() not in _AMBIGUOUS_SHORT_SCHOOL
        ]
        if not school_signals_found:
            logger.debug(
                "[INTENT] ooc_score=%d dominan, sinyal sekolah hanya dari kata ambigu → OOT",
                ooc_score,
            )
            return IntentType.OUT_OF_CONTEXT
 
    if school_score > 0 and school_score >= ooc_score:
        return IntentType.SCHOOL
 
    if ooc_score > 0 and ooc_score > school_score:
        return IntentType.OUT_OF_CONTEXT
 
    # Tidak ada sinyal keyword, tapi pola definitional → OOC
    if is_definitional:
        logger.debug("[INTENT] definitional pattern → OUT_OF_CONTEXT")
        return IntentType.OUT_OF_CONTEXT
 
    # Tidak ada sinyal jelas → biarkan router / ES yang putuskan
    return IntentType.AMBIGUOUS
 
 
# ---------------------------------------------------------------------------
# Backward-compat alias — rag_server mengimport:
#   from services.core.intent_classifier import classify_intent as rule_classify
# Alias ini menunjuk ke versi rule-based (sinkron).
# ---------------------------------------------------------------------------
classify_intent = classify_intent_rule
 
 
# ---------------------------------------------------------------------------
# Async / LLM version — re-export dari cognitive_router.
# Test mengimport:
#   from services.core.intent_classifier import classify_intent
# dan memanggil: await classify_intent(query, use_cache=False, timeout=6.0)
# Karena Python tidak bisa punya dua nama yang sama di satu modul,
# kita override classify_intent dengan versi async di bawah ini.
# rag_server sudah import LLM version langsung dari cognitive_router,
# jadi override ini tidak mempengaruhi rag_server.
# ---------------------------------------------------------------------------
try:
    from app.services.core.cognitive_router import classify_intent  # noqa: F811  # async LLM version
except ImportError:
    # Fallback ke rule-based jika cognitive_router belum tersedia (circular, dll)
    pass
