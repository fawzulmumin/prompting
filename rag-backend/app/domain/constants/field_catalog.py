"""
SINGLE SOURCE OF TRUTH untuk nilai valid field Elasticsearch.
Dipakai oleh: Tier2 classifier, aggregation_handler (distribusi patterns),
              response formatter, fuzzy matching, dan endpoint GET /fields.

Nilai di sini adalah hasil query aggregation aktual ke idx_sekolah_profile.
"""
from typing import Dict, List, Any

FIELD_CATALOG: Dict[str, Dict[str, Any]] = {
    "akreditasi": {
        "es_field": "akreditasi",
        "label": "Akreditasi sekolah",
        "values": ["A", "B", "C", "Terakreditasi", "Tidak Terakreditasi"],
        "aliases": {
            "TT": "Tidak Terakreditasi",
            "BELUM": "Tidak Terakreditasi",
            "-": "Tidak Terakreditasi",
            "tidak terakreditasi": "Tidak Terakreditasi",
            "belum terakreditasi": "Tidak Terakreditasi",
        },
        "index": "both",
        "distribusi": True,
        "notes": "Nilai 'Terakreditasi' (tanpa keterangan A/B/C) ada ~507 dokumen.",
    },
    "bentuk_pendidikan": {
        "es_field": "bentuk_pendidikan",
        "label": "Jenjang / bentuk pendidikan",
        "values": [
            "SD", "TK", "KB", "SMP", "RA", "MI", "MTs", "SPS",
            "SMA", "SMK", "Kursus", "PKBM", "MA", "Universitas",
            "Sekolah Tinggi", "PAUDQ", "TPA", "SLB", "Pondok Pesantren",
            "Akademik", "Institut", "SKB", "Politeknik",
        ],
        "aliases": {
            "pesantren": "Pondok Pesantren",
            "paud": "PAUDQ",
            "slb": "SLB",
        },
        "index": "both",
        "distribusi": False,
        "notes": "Total ada 50 nilai di ES. Hanya yang >1000 dokumen ditampilkan di /fields.",
    },
    "status_sekolah": {
        "es_field": "status_sekolah",
        "label": "Status kepemilikan (negeri/swasta)",
        "values": ["NEGERI", "SWASTA"],
        "aliases": {
            "Negeri": "NEGERI",
            "negeri": "NEGERI",
            "Swasta": "SWASTA",
            "swasta": "SWASTA",
            "n": "NEGERI",
            "s": "SWASTA",
        },
        "dirty_values": ["Luar Negeri", "Aktif", "Alih Bentuk", "Tutup", "Lainnya"],
        "index": "both",
        "distribusi": False,
        "notes": "Ada inkonsistensi kapitalisasi di ES (NEGERI vs Negeri). "
                 "Filter harus pakai terms query dengan kedua varian.",
    },
    "status_kepemilikan": {
        "es_field": "status_kepemilikan",
        "label": "Status kepemilikan lahan/gedung sekolah",
        "values": ["Yayasan", "Pemerintah Daerah", "Lainnya", "Pemerintah Pusat"],
        "aliases": {
            "yayasan": "Yayasan",
            "pemda": "Pemerintah Daerah",
            "pemerintah daerah": "Pemerintah Daerah",
            "pemerintah pusat": "Pemerintah Pusat",
            "pempus": "Pemerintah Pusat",
        },
        "index": "profile",
        "distribusi": True,
        "notes": "Coverage hampir 100% (~563K dokumen). Berbeda dari status_sekolah.",
    },
    "sumber_air": {
        "es_field": "sumber_air",
        "label": "Sumber air sekolah",
        "values": [
            "Sumur terlindungi",
            "Ledeng/PAM",
            "Pompa",
            "Mata air terlindungi",
            "Air hujan",
            "Air sungai",
            "Lainnya",
            "Sumur tidak terlindungi",
            "Air kemasan",
            "Mata air tdk terlindungi",
        ],
        "aliases": {
            "pam": "Ledeng/PAM",
            "pdam": "Ledeng/PAM",
            "ledeng": "Ledeng/PAM",
            "sumur": "Sumur terlindungi",
            "hujan": "Air hujan",
            "sungai": "Air sungai",
            "mata air": "Mata air terlindungi",
        },
        "index": "profile",
        "distribusi": True,
        "notes": "Nilai aktual ES dari idx_sekolah_profile. "
                 "Catalog lama salah (punya 'PAM/PDAM', 'Sumur' dll).",
    },
    "partisipasi_bos": {
        "es_field": "partisipasi_bos",
        "label": "Partisipasi BOS (Bantuan Operasional Sekolah)",
        "values": [True, False],
        "aliases": {
            "ya": True, "iya": True, "ikut": True, "peserta": True,
            "tidak": False, "bukan": False,
        },
        "type": "boolean",
        "index": "profile",
        "distribusi": False,
        "notes": "409622 sekolah ikut BOS, 7021 tidak.",
    },
    "waktu_penyelenggaraan": {
        "es_field": "waktu_penyelenggaraan",
        "label": "Waktu penyelenggaraan belajar",
        "values": ["Pagi", "Sore", "Pagi dan Sore", "Sehari Penuh/5 hari"],
        "aliases": {
            "pagi hari": "Pagi",
            "sore hari": "Sore",
            "double shift": "Pagi dan Sore",
            "sehari penuh": "Sehari Penuh/5 hari",
            "5 hari": "Sehari Penuh/5 hari",
        },
        "index": "qc",
        "distribusi": True,
        "notes": "Field ada di QC index, bukan profile. "
                 "Nilai aktual ES dari sample: 'Sehari Penuh/5 hari'. "
                 "Perlu aggregation untuk daftar lengkap nilai.",
    },
}

# Field yang ada di ES tapi coverage rendah atau tidak informatif untuk query user.
# Didokumentasikan untuk referensi developer; tidak masuk ke /fields default.
_LOW_COVERAGE_FIELDS: Dict[str, str] = {
    "sumber_listrik": "Coverage <0.04% di profile index. Data lebih lengkap di QC index tapi tetap sparse.",
    "akses_internet": "Coverage <0.01%. Nilai adalah nama provider lama (Telkom Speedy, dll), tidak konsisten.",
    "keaktifan": "Hanya nilai '1'. Tidak informatif untuk filter.",
    "minum_siswa": "Hanya nilai 0. Tidak informatif.",
    "kecukupan_air": "Integer code (0/1/2). Perlu decoding: 0=tidak ada, 1=kurang, 2=cukup.",
    "tipe_jamban": "Integer code. Perlu decoding: 1=leher angsa, 2=cubluk, 3=plengsengan, 4=tidak ada, 9=lainnya.",
    "kondisi_bangunan": "Tidak ditemukan di ES. False positive dari Phase 23B — jangan digunakan.",
    "kepemilikan_lahan": "Tidak ditemukan di ES. False positive dari Phase 23B — jangan digunakan.",
    "total_komputer": "Ada di mapping idx_sekolah_profile tapi tidak diisi saat indexing. "
                      "Nilai null untuk hampir semua dokumen — tidak bisa dipakai untuk range filter.",
}

DISTRIBUSI_FIELDS: List[str] = [
    k for k, v in FIELD_CATALOG.items() if v.get("distribusi")
]


def normalize_field_value(field: str, raw_value: str) -> str:
    """
    Normalize nilai user ke nilai ES yang valid.
    Cek exact match (case-insensitive), lalu aliases, lalu fuzzy (rapidfuzz, threshold 80).
    Return raw_value jika tidak ada match.
    """
    catalog = FIELD_CATALOG.get(field)
    if not catalog:
        return raw_value

    if catalog.get("type") == "boolean":
        return raw_value

    upper = raw_value.strip().upper()

    for valid in catalog["values"]:
        if isinstance(valid, str) and valid.upper() == upper:
            return valid

    for alias, canonical in catalog["aliases"].items():
        if isinstance(alias, str) and alias.upper() == upper:
            return canonical if isinstance(canonical, str) else raw_value

    try:
        from rapidfuzz import process, fuzz
        str_values = [v for v in catalog["values"] if isinstance(v, str)]
        str_aliases = [k for k in catalog["aliases"] if isinstance(k, str)]
        candidates = str_values + str_aliases
        result = process.extractOne(raw_value, candidates, scorer=fuzz.ratio)
        if result and result[1] >= 80:
            best = result[0]
            mapped = catalog["aliases"].get(best, best)
            return mapped if isinstance(mapped, str) else raw_value
    except Exception:
        pass

    return raw_value
