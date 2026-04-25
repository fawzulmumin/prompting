# Single source of truth untuk mapping jenjang pendidikan → nilai ES bentuk_pendidikan.
# Semua modul yang butuh lookup jenjang harus import dari sini.

JENJANG_MAP: dict[str, str] = {
    # Dasar
    "sd": "SD",   "sdn": "SD",   "mi": "MI",   "min": "MI",
    # Menengah pertama
    "smp": "SMP", "smpn": "SMP", "mts": "MTs", "mtsn": "MTs",
    # Menengah atas
    "sma": "SMA", "sman": "SMA", "ma": "MA",   "man": "MA",
    "smk": "SMK", "smkn": "SMK",
    # Anak usia dini
    "tk": "TK",   "tkn": "TK",   "paud": "PAUD",
    "kb": "KB",   "ra": "RA",    "sps": "SPS",  "tpa": "TPA",
    # Kebutuhan khusus & non-formal
    "slb": "SLB", "pkbm": "PKBM", "skb": "SKB",
    "kursus": "Kursus", "lkp": "LKP",
    # SPK (Satuan Pendidikan Kerjasama)
    "spk": "SPK SMA",     "spk sd": "SPK SD",    "spk smp": "SPK SMP",
    "spk sma": "SPK SMA", "spk smk": "SPK SMK",
    "spk tk": "SPK TK",   "spk kb": "SPK KB",
}

# Frozenset semua token yang dikenal — dipakai untuk deteksi kehadiran jenjang dalam query.
JENJANG_KEYWORDS: frozenset[str] = frozenset(JENJANG_MAP.keys())
