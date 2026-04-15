# profile_builder.py
#
# Referensi mapping: /rag-backend/docs/es_structure.md
#
# Aturan: selalu baca field numerik/detail dari `source` (_source ES).
# `parsed` (content JSON) hanya berisi ringkasan teks — JANGAN andalkan
# untuk siswa_l/p, tendik, nilai_akhir, kode_pos, atau guru breakdown.

from typing import Any, Dict, Optional


# ==========================================================
# UTILITIES
# ==========================================================

def _safe_int(value: Any) -> int:
    try:
        if value in (None, "", "null", "-"):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value in (None, "", "null", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


# ==========================================================
# STUDENT EXTRACTION  (baca langsung dari _source)
# ==========================================================

def extract_student_summary(source: Dict[str, Any]) -> Dict[str, Any]:
    """
    Baca data peserta didik dari _source ES.

    Field yang digunakan (lihat es_structure.md):
      siswa_l, siswa_p, siswa_total,
      siswa_baru_l, siswa_baru_p,
      siswa_lulus_l, siswa_lulus_p,
      siswa_mengulang_l, siswa_mengulang_p
    """
    siswa_l = _safe_int(source.get("siswa_l"))
    siswa_p = _safe_int(source.get("siswa_p"))
    siswa_total = _safe_int(source.get("siswa_total"))

    # Hitung total dari breakdown jika field siswa_total tidak ada
    if siswa_total == 0 and (siswa_l + siswa_p) > 0:
        siswa_total = siswa_l + siswa_p

    return {
        "total_siswa": siswa_total,
        "laki_laki": siswa_l,
        "perempuan": siswa_p,
        "siswa_baru": {
            "laki_laki": _safe_int(source.get("siswa_baru_l")),
            "perempuan": _safe_int(source.get("siswa_baru_p")),
        },
        "siswa_lulus": {
            "laki_laki": _safe_int(source.get("siswa_lulus_l")),
            "perempuan": _safe_int(source.get("siswa_lulus_p")),
        },
        "siswa_mengulang": {
            "laki_laki": _safe_int(source.get("siswa_mengulang_l")),
            "perempuan": _safe_int(source.get("siswa_mengulang_p")),
        },
    }


# ==========================================================
# GURU EXTRACTION  (baca langsung dari _source)
# ==========================================================

def extract_guru_summary(source: Dict[str, Any]) -> Dict[str, Any]:
    """
    Baca data guru dari _source ES.

    Field yang digunakan (lihat es_structure.md):
      guru_l, guru_p, guru_total,
      guru_pns, guru_p3k, guru_honor,
      guru_sertifikasi, guru_s1_keatas
    """
    guru_l = _safe_int(source.get("guru_l"))
    guru_p = _safe_int(source.get("guru_p"))
    guru_total = _safe_int(source.get("guru_total"))

    if guru_total == 0 and (guru_l + guru_p) > 0:
        guru_total = guru_l + guru_p

    return {
        "total_guru": guru_total,
        "laki_laki": guru_l,
        "perempuan": guru_p,
        "guru_pns": _safe_int(source.get("guru_pns")),
        "guru_p3k": _safe_int(source.get("guru_p3k")),
        "guru_honor": _safe_int(source.get("guru_honor")),
        "guru_sertifikasi": _safe_int(source.get("guru_sertifikasi")),
        "guru_s1_keatas": _safe_int(source.get("guru_s1_keatas")),
    }


# ==========================================================
# TENDIK EXTRACTION  (baca langsung dari _source)
# ==========================================================

def extract_tendik_summary(source: Dict[str, Any]) -> Dict[str, Any]:
    """
    Baca data tenaga kependidikan dari _source ES.

    Field yang digunakan (lihat es_structure.md):
      tendik_l, tendik_p
    """
    tendik_l = _safe_int(source.get("tendik_l"))
    tendik_p = _safe_int(source.get("tendik_p"))
    return {
        "total_tendik": tendik_l + tendik_p,
        "laki_laki": tendik_l,
        "perempuan": tendik_p,
    }


# ==========================================================
# MAIN PROFILE BUILDER
# ==========================================================

def build_profile(source: Dict[str, Any], parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bangun profil sekolah lengkap dari _source ES.

    Parameter:
      source  — dict `_source` dari Elasticsearch (data lengkap)
      parsed  — dict hasil json.loads(source["content"]) sebagai fallback teks

    Prioritas: source > parsed.
    Field numerik dan detail SELALU dari source karena content hanya ringkasan.
    """
    peserta_didik = extract_student_summary(source)
    guru = extract_guru_summary(source)
    tendik = extract_tendik_summary(source)

    # kode_pos: di ES mapping index:false & doc_values:false artinya
    # tidak bisa di-filter/aggregasi, tapi TERSEDIA di _source untuk ditampilkan.
    kode_pos = source.get("kode_pos") or parsed.get("kode_pos")

    return {
        "identitas": {
            "nama": source.get("nama"),
            "npsn": source.get("npsn"),
            "status": source.get("status_sekolah"),
            "akreditasi": source.get("akreditasi") or parsed.get("akreditasi"),
            "bentuk_pendidikan": (
                source.get("bentuk_pendidikan") or parsed.get("bentuk_pendidikan")
            ),
            "nilai_akhir": _safe_float(source.get("nilai_akhir")),
            "tahun_data": source.get("tahun"),
        },
        "lokasi": {
            "alamat": source.get("alamat_jalan") or parsed.get("alamat_jalan"),
            "desa": source.get("desa_kelurahan"),
            "kecamatan": source.get("kecamatan"),
            "kabupaten": source.get("kabupaten"),
            "provinsi": source.get("provinsi"),
            # kode_pos: bisa ditampilkan dari _source walau tidak bisa di-query/filter
            "kode_pos": kode_pos,
            "koordinat": {
                "lintang": source.get("lintang"),
                "bujur": source.get("bujur"),
            },
        },
        "sarana_prasarana": {
            "akses_internet": source.get("akses_internet") or parsed.get("akses_internet"),
            "sumber_listrik": source.get("sumber_listrik") or parsed.get("sumber_listrik"),
            "sumber_air": source.get("sumber_air") or parsed.get("sumber_air"),
            "tipe_jamban": _safe_int(source.get("tipe_jamban")),
            "toilet_siswa_laki": _safe_int(source.get("toilet_siswa_laki")),
            "toilet_siswa_perempuan": _safe_int(source.get("toilet_siswa_perempuan")),
            "total_komputer": _safe_int(source.get("total_komputer")),
            "kecukupan_air": _safe_int(source.get("kecukupan_air")),
            "minum_siswa": _safe_int(source.get("minum_siswa")),
        },
        "tenaga_pendidik": guru,
        "tenaga_kependidikan": tendik,
        "peserta_didik": peserta_didik,
        "legalitas": {
            "sk_penetapan": source.get("sk_penetapan") or parsed.get("sk_penetapan"),
            "last_update": source.get("last_update_sekolah") or parsed.get("last_update_sekolah"),
        },
    }


# ==========================================================
# PROFILE COMPLETENESS
# ==========================================================

def compute_profile_completeness(profile: Dict[str, Any]) -> float:
    """Hitung persentase kelengkapan profile (0.0 – 1.0)."""

    def count_fields(obj: Any):
        total = 0
        filled = 0
        if isinstance(obj, dict):
            for v in obj.values():
                t, f = count_fields(v)
                total += t
                filled += f
        else:
            total = 1
            filled = 0 if obj in (None, "", "null", "-", 0) else 1
        return total, filled

    total_fields, filled_fields = count_fields(profile)
    if total_fields == 0:
        return 0.0
    return round(filled_fields / total_fields, 2)
