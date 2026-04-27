# utils/location_utils.py
"""
Shared location utilities — dipakai oleh list_school_handler,
comparison_handler, dan handler lain yang butuh fuzzy location matching.
"""
import re
import logging
from typing import Optional, Tuple
 
logger = logging.getLogger(__name__)
 
# Kata Indonesia umum yang TIDAK boleh di-fuzzy-match ke nama lokasi
_FUZZY_BLACKLIST = {
    "negeri", "swasta", "untuk", "jenjang", "mana", "yang",
    "dari", "dengan", "sekolah", "jumlah", "berapa", "total",
    "kabupaten", "kecamatan", "provinsi", "kota", "lebih",
    "paling", "terbanyak", "siswa", "murid", "guru",
    # Kata umum Indonesia yang sering disalah-koreksi ke nama desa/kecamatan
    "tahun", "apakah", "adalah", "karena", "ketika", "pernah",
    "semua", "setiap", "dalam", "hanya", "bukan", "kalau",
    "sudah", "belum", "sedang", "sering", "sangat", "dimana",
    "bagaimana", "mengapa", "siapa", "kapan", "boleh", "tidak",
    "data", "nilai", "rata", "hasil", "angka", "score",
    # Kata yang sering salah fuzzy ke nama lokasi
    "banyak",      # → "bancak" (kecamatan Jawa Tengah)
    "sedikit", "atau", "bandingkan", "compare",
    "koordinat", "kosong", "bersih", "bermasalah",
    # Arah mata angin — bagian dari nama provinsi multi-kata
    "tengah", "timur", "barat", "utara", "selatan",
}

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
 
 
def fuzzy_match_location(
    keyword: str,
    levels: Optional[list] = None,
    threshold: Optional[int] = None,
) -> Optional[Tuple[str, str]]:
    """
    Cari lokasi yang paling mirip dengan keyword menggunakan Levenshtein distance.
 
    Args:
        keyword : kata yang dicari (sudah lowercase, sudah stripped)
        levels  : list level yang dicek, default ["kecamatan", "kabupaten", "provinsi"]
        threshold: max distance yang diterima, default min(2, len(keyword)//4)
 
    Returns:
        (matched_name, level) atau None jika tidak ada yang cukup dekat.
 
    Contoh:
        fuzzy_match_location("parunpanjang") → ("parungpanjang", "kecamatan")
        fuzzy_match_location("ciputat")      → ("ciputat", "kecamatan")
    """
    try:
        from app.services.location_service import LOCATION_INDEX
    except ImportError:
        logger.warning("[FUZZY] Cannot import LOCATION_INDEX")
        return None
 
    if not keyword or len(keyword) < 4:
        return None
 
    # Jangan fuzzy match kata yang mengandung spasi (phrase matching beda)
    if " " in keyword:
        return None
 
    # Protected names: kata valid yang mirip nama lain tapi BUKAN typo
    # Contoh: 'kebayoran' (Jakarta Selatan) ≠ 'kemayoran' (Jakarta Pusat)
    _PROTECTED = {
        "kebayoran", "kebumen", "purwakarta", "purwokerto",
        "karawang", "kerawang", "bandungan", "bandung",
        "cianjur", "ciamis", "ciawi", "cibadak",
    }
    if keyword.lower() in _PROTECTED:
        return None
 
    if levels is None:
        levels = ["kecamatan", "kabupaten", "provinsi", "desa_kelurahan"]
 
    if threshold is None:
        threshold = min(2, max(1, len(keyword) // 4))
 
    best_match = None
    best_dist  = 999
 
    for level in levels:
        lookup = LOCATION_INDEX.get(level, {})
        for key in lookup:
            # Bersihkan prefix ("kec.", "kab.", "prov.", "kota")
            key_clean = re.sub(
                r"^(prov\.|kab\.|kota|kec\.)\s*", "", key
            ).strip()
            # Hanya bandingkan kata tunggal
            if not key_clean or len(key_clean.split()) != 1:
                continue
            dist = _levenshtein(keyword, key_clean)
            if dist <= threshold and dist < best_dist:
                best_dist  = dist
                best_match = (key_clean, level)
 
    if best_match:
        logger.info(
            "[FUZZY] '%s' → '%s' (level=%s dist=%d)",
            keyword, best_match[0], best_match[1], best_dist
        )
 
    return best_match

# Bigram nama provinsi yang komponennya tidak boleh di-fuzzy sendiri
# Contoh: "jawa tengah" → "jawa" dan "tengah" sama-sama dilindungi
_PROTECTED_BIGRAMS: frozenset = frozenset({
    "jawa tengah", "jawa timur", "jawa barat",
    "kalimantan tengah", "kalimantan timur", "kalimantan barat",
    "kalimantan utara", "kalimantan selatan",
    "sulawesi tengah", "sulawesi timur", "sulawesi barat",
    "sulawesi utara", "sulawesi selatan",
    "nusa tenggara barat", "nusa tenggara timur",
    "papua barat", "papua tengah", "papua selatan",
    "maluku utara",
    "sumatera barat", "sumatera utara", "sumatera selatan",
})


def fuzzy_correct_words(text: str) -> str:
    """
    Koreksi typo kata per kata menggunakan fuzzy_match_location,
    dengan guard blacklist agar tidak salah koreksi kata umum.
    """
    if not text:
        return text

    words = text.split()
    text_lower = text.lower()

    # Tandai indeks kata yang merupakan bagian dari bigram provinsi
    protected_indices: set = set()
    for bigram in _PROTECTED_BIGRAMS:
        if bigram in text_lower:
            bigram_words = bigram.split()
            for i in range(len(words) - len(bigram_words) + 1):
                if all(words[i + j].lower() == bigram_words[j] for j in range(len(bigram_words))):
                    for j in range(len(bigram_words)):
                        protected_indices.add(i + j)

    corrected = []

    for idx, word in enumerate(words):

        # ✅ GUARD: bagian dari bigram provinsi — jangan diubah
        if idx in protected_indices:
            corrected.append(word)
            continue

        # ✅ GUARD: skip kata umum
        if word.lower() in _FUZZY_BLACKLIST:
            corrected.append(word)
            continue

        # hanya fuzzy untuk kata tanpa spasi & cukup panjang
        if len(word) < 4:
            corrected.append(word)
            continue

        match = fuzzy_match_location(word.lower())

        if match:
            corrected.append(match[0])  # ambil hasil koreksi
        else:
            corrected.append(word)

    return " ".join(corrected)
