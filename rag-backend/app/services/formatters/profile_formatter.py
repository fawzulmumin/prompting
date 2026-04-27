# profile_formatter.py
#
# Referensi field: /rag-backend/docs/es_structure.md
# Field source: profile_builder.py::build_profile()
#
# Struktur profile yang masuk:
#   identitas     → nama, npsn, bentuk_pendidikan, status_sekolah, keaktifan,
#                   akreditasi, waktu_penyelenggaraan, sertifikasi_iso
#   lokasi        → alamat_jalan, desa_kelurahan, kecamatan, kabupaten, provinsi,
#                   kode_pos, kode_wilayah, lintang, bujur,
#                   wilayah_terpencil, wilayah_perbatasan
#   kontak        → nomor_telepon, email, website
#   infrastruktur → sumber_listrik, akses_internet
#   kepemilikan   → status_kepemilikan, yayasan, partisipasi_bos
#   legalitas     → sk_pendirian_sekolah, tanggal_sk_pendirian,
#                   sk_izin_operasional, tanggal_sk_izin_operasional
#   kualitas_data → qc_labels{qc_1..qc_13}, total_masalah, persen_bersih

_QC_NAMES = {
    "qc_1":  "Keunikan sekolah",
    "qc_2":  "Format NPSN",
    "qc_3":  "Nama sekolah",
    "qc_4":  "Alamat jalan",
    "qc_5":  "Kode wilayah",
    "qc_6":  "Lintang",
    "qc_7":  "Bujur",
    "qc_8":  "SK izin operasional",
    "qc_9":  "Tanggal SK izin",
    "qc_10": "SK pendirian",
    "qc_11": "Tanggal SK pendirian",
    "qc_12": "Foto sekolah",
    "qc_13": "Data yayasan (swasta)",
}


def safe_count(value) -> str:
    """Untuk field numerik: 0 adalah nilai valid, bukan 'tidak ada data'."""
    if value in (None, "", "null"):
        return "-"
    return str(value)


def format_profile_markdown(profile: dict) -> str:
    identitas  = profile.get("identitas", {})
    lokasi     = profile.get("lokasi", {})
    kontak     = profile.get("kontak", {})
    infra      = profile.get("infrastruktur", {})
    kepemilikan = profile.get("kepemilikan", {})
    legalitas  = profile.get("legalitas", {})
    qc         = profile.get("kualitas_data", {})

    # ── Identitas ──────────────────────────────────────────────────────────────
    markdown = f"""### Profil Sekolah Lengkap

## Identitas
- **Nama:** {safe_count(identitas.get("nama"))}
- **NPSN:** {safe_count(identitas.get("npsn"))}
- **Bentuk Pendidikan:** {safe_count(identitas.get("bentuk_pendidikan"))}
- **Status Sekolah:** {safe_count(identitas.get("status_sekolah"))}
- **Keaktifan:** {safe_count(identitas.get("keaktifan"))}
- **Akreditasi:** {safe_count(identitas.get("akreditasi"))}
- **Waktu Penyelenggaraan:** {safe_count(identitas.get("waktu_penyelenggaraan"))}
- **Sertifikasi ISO:** {safe_count(identitas.get("sertifikasi_iso"))}

---

## Lokasi
- **Alamat:** {safe_count(lokasi.get("alamat_jalan"))}
- **Desa/Kelurahan:** {safe_count(lokasi.get("desa_kelurahan"))}
- **Kecamatan:** {safe_count(lokasi.get("kecamatan"))}
- **Kabupaten/Kota:** {safe_count(lokasi.get("kabupaten"))}
- **Provinsi:** {safe_count(lokasi.get("provinsi"))}
- **Kode Pos:** {safe_count(lokasi.get("kode_pos"))}
- **Kode Wilayah:** {safe_count(lokasi.get("kode_wilayah"))}
- **Koordinat:** {safe_count(lokasi.get("lintang"))}, {safe_count(lokasi.get("bujur"))}
- **Wilayah Terpencil:** {safe_count(lokasi.get("wilayah_terpencil"))}
- **Wilayah Perbatasan:** {safe_count(lokasi.get("wilayah_perbatasan"))}

---

## Kontak
- **Telepon:** {safe_count(kontak.get("nomor_telepon"))}
- **Email:** {safe_count(kontak.get("email"))}
- **Website:** {safe_count(kontak.get("website"))}

---

## Kepemilikan & Fasilitas
- **Status Kepemilikan:** {safe_count(kepemilikan.get("status_kepemilikan"))}
- **Yayasan:** {safe_count(kepemilikan.get("yayasan"))}
- **Partisipasi BOS:** {safe_count(kepemilikan.get("partisipasi_bos"))}
- **Sumber Listrik:** {safe_count(infra.get("sumber_listrik"))}
- **Akses Internet:** {safe_count(infra.get("akses_internet"))}

---

## Legalitas
- **SK Pendirian:** {safe_count(legalitas.get("sk_pendirian_sekolah"))}
- **Tanggal SK Pendirian:** {safe_count(legalitas.get("tanggal_sk_pendirian"))}
- **SK Izin Operasional:** {safe_count(legalitas.get("sk_izin_operasional"))}
- **Tanggal SK Izin Operasional:** {safe_count(legalitas.get("tanggal_sk_izin_operasional"))}

---

## Kualitas Data
"""

    # Baris ringkasan skor QC
    persen_bersih  = qc.get("persen_bersih")
    total_masalah  = qc.get("total_masalah")
    persen_str     = f"{persen_bersih}%" if persen_bersih is not None else "-"
    masalah_str    = str(total_masalah) if total_masalah is not None else "-"
    markdown += f"- **Skor:** {persen_str} bersih | {masalah_str} kolom bermasalah\n"

    # Detail per kolom QC
    qc_labels = qc.get("qc_labels") or {}
    _status_sekolah = (identitas.get("status_sekolah") or "").upper()
    for field, name in _QC_NAMES.items():
        if field == "qc_13" and _status_sekolah == "NEGERI":
            continue
        status = qc_labels.get(field)
        markdown += f"- **{name}:** {safe_count(status)}\n"

    return markdown
