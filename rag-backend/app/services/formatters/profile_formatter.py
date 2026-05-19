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


def _format_qc_status(value) -> str:
    """Format nilai QC field menjadi label human-readable dengan emoji."""
    if value is None or value == "" or value == "null":
        return "- (data tidak tersedia)"

    v_str = str(value).strip()
    v_lower = v_str.lower()

    # profile_builder menghasilkan "OK" atau "Bermasalah (kode: N)"
    if v_lower == "ok" or v_lower in ("1", "baik", "valid", "bersih"):
        return "✅ OK"
    if v_lower.startswith("bermasalah"):
        return f"⚠️ {v_str}"

    # Fallback: coba parse sebagai integer (0 = OK, >0 = bermasalah)
    try:
        int_val = int(value)
        return "✅ OK" if int_val == 0 else f"⚠️ Bermasalah (nilai: {int_val})"
    except (ValueError, TypeError):
        pass

    return v_str


def _has_any(d: dict) -> bool:
    """True jika ada field non-None dalam dict."""
    return any(v is not None for v in d.values())


def format_profile_markdown(profile: dict) -> str:
    identitas   = profile.get("identitas", {})
    lokasi      = profile.get("lokasi", {})
    kontak      = profile.get("kontak", {})
    infra       = profile.get("infrastruktur", {})
    kepemilikan = profile.get("kepemilikan", {})
    legalitas   = profile.get("legalitas", {})
    qc          = profile.get("kualitas_data", {})
    siswa       = profile.get("siswa", {})
    guru        = profile.get("guru", {})
    tendik      = profile.get("tendik", {})
    sarana      = profile.get("sarana_prasarana", {})

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
"""

    # ── Peserta Didik ──────────────────────────────────────────────────────────
    if _has_any(siswa):
        markdown += "\n## Peserta Didik\n"
        markdown += f"- **Total Siswa:** {safe_count(siswa.get('total'))}\n"
        markdown += f"- **Laki-laki / Perempuan:** {safe_count(siswa.get('laki_laki'))} / {safe_count(siswa.get('perempuan'))}\n"
        if siswa.get("baru_laki") is not None or siswa.get("baru_perempuan") is not None:
            markdown += f"- **Siswa Baru L/P:** {safe_count(siswa.get('baru_laki'))} / {safe_count(siswa.get('baru_perempuan'))}\n"
        if siswa.get("lulus_laki") is not None or siswa.get("lulus_perempuan") is not None:
            markdown += f"- **Lulusan L/P:** {safe_count(siswa.get('lulus_laki'))} / {safe_count(siswa.get('lulus_perempuan'))}\n"
        markdown += "\n---\n"

    # ── Guru & Tendik ──────────────────────────────────────────────────────────
    if _has_any(guru) or _has_any(tendik):
        markdown += "\n## Guru & Tenaga Kependidikan\n"
        if _has_any(guru):
            markdown += f"- **Total Guru:** {safe_count(guru.get('total'))}\n"
            markdown += f"- **Guru L/P:** {safe_count(guru.get('laki_laki'))} / {safe_count(guru.get('perempuan'))}\n"
            if guru.get("pns") is not None:
                markdown += f"- **PNS:** {safe_count(guru.get('pns'))}\n"
            if guru.get("p3k") is not None:
                markdown += f"- **P3K:** {safe_count(guru.get('p3k'))}\n"
            if guru.get("honor") is not None:
                markdown += f"- **Honorer:** {safe_count(guru.get('honor'))}\n"
            if guru.get("sertifikasi") is not None:
                markdown += f"- **Bersertifikasi:** {safe_count(guru.get('sertifikasi'))}\n"
            if guru.get("s1_keatas") is not None:
                markdown += f"- **Pendidikan S1+:** {safe_count(guru.get('s1_keatas'))}\n"
        if _has_any(tendik):
            markdown += f"- **Tendik L/P:** {safe_count(tendik.get('laki_laki'))} / {safe_count(tendik.get('perempuan'))}\n"
            if tendik.get("total") is not None:
                markdown += f"- **Total Tendik:** {safe_count(tendik.get('total'))}\n"
        markdown += "\n---\n"

    # ── Sarana & Prasarana ─────────────────────────────────────────────────────
    if _has_any(sarana):
        markdown += "\n## Sarana & Prasarana\n"
        if sarana.get("tipe_jamban") is not None:
            markdown += f"- **Tipe Jamban:** {safe_count(sarana.get('tipe_jamban'))}\n"
        if sarana.get("kecukupan_air") is not None:
            markdown += f"- **Kecukupan Air:** {safe_count(sarana.get('kecukupan_air'))}\n"
        if sarana.get("sumber_air") is not None:
            markdown += f"- **Sumber Air:** {safe_count(sarana.get('sumber_air'))}\n"
        if sarana.get("minum_siswa") is not None:
            markdown += f"- **Unit Air Minum:** {safe_count(sarana.get('minum_siswa'))}\n"
        if sarana.get("toilet_laki") is not None or sarana.get("toilet_perempuan") is not None:
            markdown += f"- **Toilet L/P:** {safe_count(sarana.get('toilet_laki'))} / {safe_count(sarana.get('toilet_perempuan'))}\n"
        if sarana.get("total_komputer") is not None:
            markdown += f"- **Komputer:** {safe_count(sarana.get('total_komputer'))}\n"
        markdown += "\n---\n"

    # ── Kualitas Data ──────────────────────────────────────────────────────────
    markdown += "\n## Kualitas Data\n"

    # Baris ringkasan skor QC
    persen_bersih  = qc.get("persen_bersih")
    total_masalah  = qc.get("total_masalah")
    persen_str     = f"{persen_bersih}%" if persen_bersih is not None else "-"
    masalah_str    = str(total_masalah) if total_masalah is not None else "-"
    markdown += f"- **Skor:** {persen_str} bersih | {masalah_str} kolom bermasalah\n"

    # Detail per kolom QC
    qc_labels = qc.get("qc_labels") or {}
    _status_sekolah = (identitas.get("status_sekolah") or "").upper()

    # Summary field bermasalah — tampilkan sebelum detail per kolom
    if total_masalah is not None and int(total_masalah) > 0:
        masalah_fields = []
        for field, name in _QC_NAMES.items():
            if field == "qc_13" and _status_sekolah == "NEGERI":
                continue
            val = qc_labels.get(field)
            if val is not None and str(val).lower().startswith("bermasalah"):
                masalah_fields.append(name)
        if masalah_fields:
            markdown += f"- **Field bermasalah:** {', '.join(masalah_fields)}\n"
        else:
            markdown += "- **Field bermasalah:** (tidak teridentifikasi)\n"

    for field, name in _QC_NAMES.items():
        if field == "qc_13" and _status_sekolah == "NEGERI":
            continue
        status = qc_labels.get(field)
        markdown += f"- **{name}:** {_format_qc_status(status)}\n"

    return markdown
