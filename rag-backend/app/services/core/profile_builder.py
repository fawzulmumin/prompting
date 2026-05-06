# profile_builder.py
#
# Referensi mapping: /rag-backend/docs/es_structure.md
#
# Aturan: selalu baca field numerik/detail dari `source` (_source ES).
# `parsed` (content JSON) hanya berisi ringkasan teks — JANGAN andalkan
# untuk data numerik atau field detail lainnya.

from typing import Any, Dict, Optional


# ==========================================================
# LABEL MAPPINGS
# ==========================================================

_TIPE_JAMBAN_LABELS: Dict[int, str] = {
    0: "Tidak Ada",
    1: "Leher Angsa",
    2: "Plengsengan",
    3: "Cemplung/Cubluk",
}

_KECUKUPAN_AIR_LABELS: Dict[int, str] = {
    0: "Sangat Kurang",
    1: "Kurang",
    2: "Cukup",
    3: "Sangat Cukup",
}


def _jamban_label(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        return _TIPE_JAMBAN_LABELS.get(int(v))
    except (TypeError, ValueError):
        return None


def _air_label(v: Any) -> Optional[str]:
    if v is None:
        return None
    try:
        return _KECUKUPAN_AIR_LABELS.get(int(v))
    except (TypeError, ValueError):
        return None


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
# QC EXTRACTION  (baca langsung dari _source)
# ==========================================================

def extract_qc_summary(source: Dict[str, Any]) -> Dict[str, Any]:
    """
    Baca data kualitas data (QC) dari _source ES.

    qc: nested object dengan field qc_1..qc_13 (byte).
        Nilai 1 = OK, nilai lain = bermasalah.
    qc_score: object biasa dengan total_masalah (int) dan persen_bersih (float).
    """
    qc = source.get("qc") or {}
    qc_score = source.get("qc_score") or {}

    labels: Dict[str, str] = {}
    for i in range(1, 14):
        field = f"qc_{i}"
        val = qc.get(field)
        if val is None:
            labels[field] = None
        elif val == 1:
            labels[field] = "OK"
        else:
            labels[field] = f"Bermasalah (kode: {val})"

    return {
        "qc_labels": labels,
        "total_masalah": _safe_int(qc_score.get("total_masalah")) or None,
        "persen_bersih": _safe_float(qc_score.get("persen_bersih")),
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
    profile = {
        "identitas": {
            "nama": source.get("nama"),
            "npsn": source.get("npsn"),
            "bentuk_pendidikan": source.get("bentuk_pendidikan") or parsed.get("bentuk_pendidikan"),
            "status_sekolah": source.get("status_sekolah"),
            "keaktifan": source.get("keaktifan"),
            "akreditasi": source.get("akreditasi") or parsed.get("akreditasi"),
            "waktu_penyelenggaraan": source.get("waktu_penyelenggaraan"),
            "sertifikasi_iso": source.get("sertifikasi_iso"),
        },
        "lokasi": {
            "alamat_jalan": source.get("alamat_jalan") or parsed.get("alamat_jalan"),
            "desa_kelurahan": source.get("desa_kelurahan"),
            "kecamatan": source.get("kecamatan"),
            "kabupaten": source.get("kabupaten"),
            "provinsi": source.get("provinsi"),
            "kode_pos": source.get("kode_pos") or parsed.get("kode_pos"),
            "kode_wilayah": source.get("kode_wilayah"),
            "lintang": source.get("lintang"),
            "bujur": source.get("bujur"),
            "wilayah_terpencil": source.get("wilayah_terpencil"),
            "wilayah_perbatasan": source.get("wilayah_perbatasan"),
        },
        "kontak": {
            "nomor_telepon": source.get("nomor_telepon"),
            "email": source.get("email"),
            "website": source.get("website"),
        },
        "infrastruktur": {
            "sumber_listrik": source.get("sumber_listrik") or parsed.get("sumber_listrik"),
            "akses_internet": source.get("akses_internet") or parsed.get("akses_internet"),
        },
        "kepemilikan": {
            "status_kepemilikan": source.get("status_kepemilikan"),
            "yayasan": source.get("yayasan"),
            "partisipasi_bos": source.get("partisipasi_bos"),
        },
        "legalitas": {
            "sk_pendirian_sekolah": source.get("sk_pendirian_sekolah"),
            "tanggal_sk_pendirian": source.get("tanggal_sk_pendirian"),
            "sk_izin_operasional": source.get("sk_izin_operasional"),
            "tanggal_sk_izin_operasional": source.get("tanggal_sk_izin_operasional"),
        },
        "kualitas_data": extract_qc_summary(source),
        "sarana_prasarana": {
            "tipe_jamban": _jamban_label(source.get("tipe_jamban")),
            "kecukupan_air": _air_label(source.get("kecukupan_air")),
        },
    }

    profile["guru"] = {
        "total":       source.get("guru_total"),
        "laki_laki":   source.get("guru_l"),
        "perempuan":   source.get("guru_p"),
        "pns":         source.get("guru_pns"),
        "p3k":         source.get("guru_p3k"),
        "honor":       source.get("guru_honor"),
        "sertifikasi": source.get("guru_sertifikasi"),
        "s1_keatas":   source.get("guru_s1_keatas"),
    }

    profile["tendik"] = {
        "laki_laki": source.get("tendik_l"),
        "perempuan": source.get("tendik_p"),
        "total": (
            _safe_int(source.get("tendik_l") or 0) + _safe_int(source.get("tendik_p") or 0)
        ) or None,
    }

    profile["siswa"] = {
        "total":              source.get("siswa_total"),
        "laki_laki":          source.get("siswa_l"),
        "perempuan":          source.get("siswa_p"),
        "baru_laki":          source.get("siswa_baru_l"),
        "baru_perempuan":     source.get("siswa_baru_p"),
        "lulus_laki":         source.get("siswa_lulus_l"),
        "lulus_perempuan":    source.get("siswa_lulus_p"),
        "mengulang_laki":     source.get("siswa_mengulang_l"),
        "mengulang_perempuan": source.get("siswa_mengulang_p"),
    }

    profile["sarana_prasarana"].update({
        "tipe_jamban":      _jamban_label(source.get("tipe_jamban")),
        "kecukupan_air":    _air_label(source.get("kecukupan_air")),
        "minum_siswa":      source.get("minum_siswa"),
        "sumber_air":       source.get("sumber_air"),
        "toilet_laki":      source.get("toilet_siswa_laki"),
        "toilet_perempuan": source.get("toilet_siswa_perempuan"),
        "total_komputer":   source.get("total_komputer"),
    })

    profile["semester"] = {
        "semester_id": source.get("semester_id"),
        "tahun":       source.get("tahun"),
        "nilai_akhir": source.get("nilai_akhir"),
    }

    profile["_meta_sync"] = source.get("_meta_sync")

    return profile


# ==========================================================
# PROFILE COMPLETENESS
# ==========================================================

def compute_profile_completeness(profile: Dict[str, Any]) -> float:
    """Hitung persentase kelengkapan profile (0.0 – 1.0)."""

    def count_fields(obj: Any):
        total = 0
        filled = 0
        if isinstance(obj, dict):
            for k, v in obj.items():
                # qc_labels dihitung per entry, bukan sebagai satu field
                t, f = count_fields(v)
                total += t
                filled += f
        elif isinstance(obj, list):
            total = 1
            filled = 1 if obj else 0
        else:
            total = 1
            filled = 0 if obj in (None, "", "null", "-") else 1
        return total, filled

    # Kualitas_data tidak dihitung dalam completeness — merupakan metadata QC
    profile_without_qc = {k: v for k, v in profile.items() if k != "kualitas_data"}

    total_fields, filled_fields = count_fields(profile_without_qc)
    if total_fields == 0:
        return 0.0
    return round(filled_fields / total_fields, 2)
