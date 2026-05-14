"""Local (sync, LLM-free) narration functions.

Fast-path narasi untuk kasus sederhana yang tidak perlu LLM.
Format angka: Indonesian thousand-separator (titik) — 1.234.
"""
from typing import Optional


def _fmt(n: int) -> str:
    """Format integer dengan pemisah ribuan gaya Indonesia: 1.234.567."""
    return f"{n:,}".replace(",", ".")


def narrate_zero_result(
    wilayah: str,
    active_filters: dict = None,
    modul: str = "aggregation",
) -> str:
    """Template pesan 0-hasil. Wraps _zero_result_message dari narrator_service."""
    from app.services.narrator_service import _zero_result_message
    return _zero_result_message(wilayah, active_filters, modul)


def narrate_qc_pct(
    total_sekolah: int,
    total_bersih: int,
    total_bermasalah: int,
    persen_bersih: float,
    persen_bermasalah: float,
    wilayah_label: str = "seluruh Indonesia",
) -> str:
    """
    Narasi persentase kualitas data sekolah — template statis, tanpa LLM.

    Returns:
        String narasi siap pakai (bahasa Indonesia, angka dengan titik-ribuan).
    """
    persen_b = f"{persen_bersih:.1f}".replace(".", ",")
    persen_m = f"{persen_bermasalah:.1f}".replace(".", ",")

    return (
        f"Dari total {_fmt(total_sekolah)} sekolah di {wilayah_label}, "
        f"sebanyak {_fmt(total_bersih)} sekolah ({persen_b}%) memiliki data yang bersih. "
        f"Sementara {_fmt(total_bermasalah)} sekolah ({persen_m}%) masih memiliki "
        f"setidaknya satu masalah data yang perlu diperbaiki."
    )


def narrate_aggregation_simple(
    wilayah: str,
    total: int,
    level: str,
    jenjang: Optional[str] = None,
    status: Optional[str] = None,
    metric_target: Optional[str] = None,
) -> Optional[str]:
    """
    Narasi aggregation tanpa breakdown/drilldown — 3 kalimat, tanpa LLM.

    Hanya dipakai ketika tidak ada breakdown_jenjang, breakdown_status,
    drilldown_top3, dan metric_target adalah None atau 'sekolah'.

    Returns:
        Narasi string, atau None jika kondisi tidak memenuhi (caller harus fallback ke LLM).
    """
    _METRIC_LABELS = {
        "siswa":            "siswa",
        "siswa_l":          "siswa laki-laki",
        "siswa_p":          "siswa perempuan",
        "guru":             "guru/PTK",
        "guru_pns":         "guru PNS",
        "guru_honor":       "guru honorer",
        "guru_sertifikasi": "guru bersertifikasi",
        "tendik":           "tenaga kependidikan",
    }

    # Hanya handle sekolah-count (metric numeric butuh konteks lebih)
    if metric_target and metric_target not in (None, "sekolah"):
        return None

    _LEVEL_LABEL = {
        "desa_kelurahan": "desa/kelurahan",
        "kecamatan":      "kecamatan",
        "kabupaten":      "kabupaten/kota",
        "provinsi":       "provinsi",
        "nasional":       "nasional",
    }
    level_label = _LEVEL_LABEL.get(level, level)

    _BENCHMARK_DESA     = {"rendah": 5,    "sedang": 20,   "tinggi": 40}
    _BENCHMARK_KEC      = {"rendah": 30,   "sedang": 100,  "tinggi": 300}
    _BENCHMARK_KAB      = {"rendah": 500,  "sedang": 2000, "tinggi": 6000}
    _BENCHMARK_PROVINSI = {"rendah": 5000, "sedang": 20000,"tinggi": 60000}

    tbl = {
        "desa_kelurahan": _BENCHMARK_DESA,
        "kecamatan":      _BENCHMARK_KEC,
        "kabupaten":      _BENCHMARK_KAB,
        "provinsi":       _BENCHMARK_PROVINSI,
    }.get(level, _BENCHMARK_DESA)

    if total <= tbl["rendah"]:
        konteks = "tergolong sedikit untuk level wilayah ini"
    elif total <= tbl["sedang"]:
        konteks = "tergolong cukup memadai untuk level wilayah ini"
    else:
        konteks = "tergolong banyak dan mencerminkan layanan pendidikan yang luas"

    # Bangun filter desc
    filter_parts = []
    if jenjang:
        filter_parts.append(f"jenjang {jenjang}")
    if status:
        filter_parts.append(f"berstatus {status}")
    filter_desc = " ".join(filter_parts)
    filter_suffix = f" dengan kriteria {filter_desc}" if filter_desc else ""

    wilayah_title = wilayah.title() if wilayah else "wilayah yang dimaksud"

    kalimat1 = (
        f"Terdapat {_fmt(total)} satuan pendidikan{filter_suffix} "
        f"di {wilayah_title} (level: {level_label})."
    )
    kalimat2 = f"Jumlah ini {konteks}."
    kalimat3 = (
        "Informasi ini dapat digunakan sebagai acuan perencanaan "
        "kebutuhan layanan pendidikan di wilayah tersebut. "
        "Apabila Anda memerlukan rincian lebih lanjut seperti komposisi jenjang "
        "atau status sekolah, silakan tanyakan."
    )

    return f"{kalimat1} {kalimat2} {kalimat3}"


def narrate_single_section(merged_context: str) -> str:
    """
    Passthrough untuk merged_context yang hanya memiliki 1 section (tidak ada divider).
    Digunakan di narrate_multi_module() sebelum LLM call.
    """
    return merged_context
