# profile_formatter.py
#
# Referensi field: /rag-backend/docs/es_structure.md
# Field source: profile_builder.py::build_profile()
#
# Struktur profile yang masuk:
#   identitas  → nama, npsn, status, akreditasi, bentuk_pendidikan, nilai_akhir, tahun_data
#   lokasi     → alamat, desa, kecamatan, kabupaten, provinsi, kode_pos, koordinat
#   tenaga_pendidik     → total_guru, laki_laki, perempuan, guru_pns, guru_p3k,
#                         guru_honor, guru_sertifikasi, guru_s1_keatas
#   tenaga_kependidikan → total_tendik, laki_laki, perempuan
#   peserta_didik       → total_siswa, laki_laki, perempuan,
#                         siswa_baru{laki_laki, perempuan},
#                         siswa_lulus{laki_laki, perempuan},
#                         siswa_mengulang{laki_laki, perempuan}
#   sarana_prasarana    → akses_internet, sumber_listrik, sumber_air, tipe_jamban,
#                         toilet_siswa_laki, toilet_siswa_perempuan, total_komputer,
#                         kecukupan_air, minum_siswa
#   legalitas  → sk_penetapan, last_update


def safe(value) -> str:
    if value in (None, "", "null", 0):
        return "-"
    return str(value)


def format_profile_markdown(profile: dict) -> str:
    identitas = profile.get("identitas", {})
    lokasi    = profile.get("lokasi", {})
    guru      = profile.get("tenaga_pendidik", {})
    tendik    = profile.get("tenaga_kependidikan", {})
    siswa     = profile.get("peserta_didik", {})
    sarana    = profile.get("sarana_prasarana", {})
    legalitas = profile.get("legalitas", {})

    # ── Identitas ──────────────────────────────────────────────────────────────
    nilai_akhir_str = (
        f"\n- **Nilai Akhir Akreditasi:** {identitas['nilai_akhir']}"
        if identitas.get("nilai_akhir") not in (None, "", "-")
        else ""
    )

    markdown = f"""### Profil Sekolah Lengkap

## Identitas
- **Nama:** {safe(identitas.get("nama"))}
- **NPSN:** {safe(identitas.get("npsn"))}
- **Status:** {safe(identitas.get("status"))}
- **Akreditasi:** {safe(identitas.get("akreditasi"))}{nilai_akhir_str}
- **Bentuk Pendidikan:** {safe(identitas.get("bentuk_pendidikan"))}
- **Tahun Data:** {safe(identitas.get("tahun_data"))}

---

## Lokasi
- **Alamat:** {safe(lokasi.get("alamat"))}
- **Desa/Kelurahan:** {safe(lokasi.get("desa"))}
- **Kecamatan:** {safe(lokasi.get("kecamatan"))}
- **Kabupaten/Kota:** {safe(lokasi.get("kabupaten"))}
- **Provinsi:** {safe(lokasi.get("provinsi"))}
- **Kode Pos:** {safe(lokasi.get("kode_pos"))}
- **Koordinat:** {safe(lokasi.get("koordinat", {}).get("lintang"))}, {safe(lokasi.get("koordinat", {}).get("bujur"))}

---

## Tenaga Pendidik (Guru)
- **Total Guru:** {safe(guru.get("total_guru"))} (L: {safe(guru.get("laki_laki"))}, P: {safe(guru.get("perempuan"))})
- **Guru PNS:** {safe(guru.get("guru_pns"))}
- **Guru P3K:** {safe(guru.get("guru_p3k"))}
- **Guru Honor:** {safe(guru.get("guru_honor"))}
- **Guru Sertifikasi:** {safe(guru.get("guru_sertifikasi"))}
- **Guru S1 ke Atas:** {safe(guru.get("guru_s1_keatas"))}

---

## Tenaga Kependidikan (Tendik)
- **Total Tendik:** {safe(tendik.get("total_tendik"))} (L: {safe(tendik.get("laki_laki"))}, P: {safe(tendik.get("perempuan"))})

---

## Peserta Didik
- **Total Siswa:** {safe(siswa.get("total_siswa"))} (L: {safe(siswa.get("laki_laki"))}, P: {safe(siswa.get("perempuan"))})
"""

    # Rincian dinamis: baru, lulus, mengulang
    _rincian = {
        "Siswa Baru":      siswa.get("siswa_baru", {}),
        "Siswa Lulus":     siswa.get("siswa_lulus", {}),
        "Siswa Mengulang": siswa.get("siswa_mengulang", {}),
    }
    for label, sub in _rincian.items():
        l_val = sub.get("laki_laki", 0) or 0
        p_val = sub.get("perempuan", 0) or 0
        if l_val + p_val > 0:
            markdown += f"- **{label}:** {l_val + p_val} (L: {l_val}, P: {p_val})\n"

    # ── Sarana & Prasarana ─────────────────────────────────────────────────────
    markdown += f"""
---

## Sarana & Prasarana
- **Akses Internet:** {safe(sarana.get("akses_internet"))}
- **Sumber Listrik:** {safe(sarana.get("sumber_listrik"))}
- **Sumber Air:** {safe(sarana.get("sumber_air"))}
- **Kecukupan Air:** {safe(sarana.get("kecukupan_air"))}
- **Tipe Jamban:** {safe(sarana.get("tipe_jamban"))}
- **Toilet Laki-laki:** {safe(sarana.get("toilet_siswa_laki"))}
- **Toilet Perempuan:** {safe(sarana.get("toilet_siswa_perempuan"))}
- **Total Komputer:** {safe(sarana.get("total_komputer"))}

---

## Legalitas
- **SK Penetapan:** {safe(legalitas.get("sk_penetapan"))}
- **Terakhir Update:** {safe(legalitas.get("last_update"))}
"""

    return markdown
