import re
from typing import Dict, Any


# ==========================================================
# UTILITIES
# ==========================================================

def _safe_int(value):
    try:
        if value in (None, "", "null", "-"):
            return 0
        return int(value)
    except Exception:
        return 0


def _safe_sum(a, b):
    return _safe_int(a) + _safe_int(b)


# ==========================================================
# STUDENT EXTRACTION
# ==========================================================

def extract_student_summary(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Universal student extractor:
    - Menangkap semua field dengan pola *_l dan *_p
    - Menghindari double count
    - Support semua jenjang (TK, SD, SMP, SMA, dll)
    """

    total_l = 0
    total_p = 0
    detail = {}

    pattern = re.compile(r"^pd_.*_(l|p)$")

    for key, value in parsed.items():
        if not pattern.match(key):
            continue

        gender = key.split("_")[-1]

        val = _safe_int(value)

        if gender == "l":
            total_l += val
        elif gender == "p":
            total_p += val

        detail[key] = val

    total_all = total_l + total_p

    # fallback kalau memang tidak ada breakdown
    if total_all == 0:
        total_all = _safe_int(
            parsed.get("jumlah_pd")
            or parsed.get("total_siswa")
            or parsed.get("pd_total")
        )

    return {
        "total_siswa": total_all,
        "laki_laki": total_l,
        "perempuan": total_p,
        "raw_breakdown_count": len(detail)
    }


# ==========================================================
# MAIN PROFILE BUILDER
# ==========================================================

def build_profile(source: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:

    student_summary = extract_student_summary(parsed)  

    total_ptk = _safe_sum(parsed.get("ptk_l"), parsed.get("ptk_p"))

    profile = {
        "identitas": {
            "nama": source.get("nama"),
            "npsn": source.get("npsn"),
            "status": source.get("status_sekolah"),
            "akreditasi": parsed.get("akreditasi") or source.get("akreditasi"),
            "bentuk_pendidikan": parsed.get("bentuk_pendidikan") or source.get("bentuk_pendidikan"),
            "tahun_data": source.get("tahun"),
        },
        "lokasi": {
            "alamat": parsed.get("alamat_jalan"),
            "desa": source.get("desa_kelurahan"),
            "kecamatan": source.get("kecamatan"),
            "kabupaten": source.get("kabupaten"),
            "provinsi": source.get("provinsi"),
            "kode_pos": parsed.get("kode_pos"),
            "koordinat": {
                "lintang": source.get("lintang"),
                "bujur": source.get("bujur"),
            },
        },
        "sarana_prasarana": {
            "akses_internet": parsed.get("akses_internet"),
            "sumber_listrik": parsed.get("sumber_listrik"),
            "sumber_air": parsed.get("sumber_air"),
            "tipe_jamban": parsed.get("tipe_jamban"),
        },
        "tenaga_pendidik": {
            "total_ptk": total_ptk,
            "ptk_l": _safe_int(parsed.get("ptk_l")),
            "ptk_p": _safe_int(parsed.get("ptk_p")),
            "guru_pns": _safe_sum(parsed.get("ptk_guru_pns_l"), parsed.get("ptk_guru_pns_p")),
            "guru_honor": _safe_sum(parsed.get("ptk_guru_honor_l"), parsed.get("ptk_guru_honor_p")),
            "guru_sertifikasi": _safe_sum(
                parsed.get("ptk_guru_sertifikasi_l"),
                parsed.get("ptk_guru_sertifikasi_p"),
            ),
        },
        "peserta_didik": student_summary,
        "legalitas": {
            "sk_penetapan": parsed.get("sk_penetapan"),
            "last_update": parsed.get("last_update_sekolah"),
        },
    }

    return profile


# ==========================================================
# PROFILE COMPLETENESS
# ==========================================================

def compute_profile_completeness(profile: Dict[str, Any]) -> float:
    """
    Hitung persentase kelengkapan profile
    """

    def count_fields(obj):
        total = 0
        filled = 0

        if isinstance(obj, dict):
            for v in obj.values():
                t, f = count_fields(v)
                total += t
                filled += f
        else:
            total = 1
            if obj not in (None, "", "null", "-", 0):
                filled = 1
            else:
                filled = 0

        return total, filled

    total_fields, filled_fields = count_fields(profile)

    if total_fields == 0:
        return 0.0

    return round(filled_fields / total_fields, 2)
