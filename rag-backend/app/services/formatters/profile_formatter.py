def format_profile_markdown(profile: dict) -> str:

    identitas = profile["identitas"]
    lokasi = profile["lokasi"]
    tenaga = profile["tenaga_pendidik"]
    siswa = profile["peserta_didik"]
    sarana = profile["sarana_prasarana"]
    legalitas = profile.get("legalitas", {})

    markdown = f"""### Profil Sekolah Lengkap

## Identitas
- **Nama:** {safe(identitas.get("nama"))}
- **NPSN:** {safe(identitas.get("npsn"))}
- **Status:** {safe(identitas.get("status"))}
- **Akreditasi:** {safe(identitas.get("akreditasi"))}
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

## Tenaga Pendidik
- **Total PTK:** {safe(tenaga.get("total_ptk"))}
- **PTK Laki-laki:** {safe(tenaga.get("ptk_l"))}
- **PTK Perempuan:** {safe(tenaga.get("ptk_p"))}
- **Guru PNS:** {safe(tenaga.get("guru_pns"))}
- **Guru Honor:** {safe(tenaga.get("guru_honor"))}
- **Guru Sertifikasi:** {safe(tenaga.get("guru_sertifikasi"))}

---

## Peserta Didik
- **Total Siswa:** {safe(siswa.get("total_siswa"))}
"""

    # =========================
    # PER KELAS
    # =========================
    per_kelas = siswa.get("per_kelas", {})

    if per_kelas:
        markdown += "\n### Rincian Per Kelas\n"
        for kelas, data in sorted(per_kelas.items()):
            total = (data.get("l", 0) or 0) + (data.get("p", 0) or 0)
            markdown += (
                f"- **Kelas {kelas}:** {total} "
                f"(L: {data.get('l', 0)}, P: {data.get('p', 0)})\n"
            )

    # =========================
    # SARANA
    # =========================
    markdown += f"""

---

## Sarana & Prasarana
- **Akses Internet:** {safe(sarana.get("akses_internet"))}
- **Sumber Listrik:** {safe(sarana.get("sumber_listrik"))}
- **Sumber Air:** {safe(sarana.get("sumber_air"))}
- **Tipe Jamban:** {safe(sarana.get("tipe_jamban"))}
"""

    # =========================
    # LEGALITAS
    # =========================
    markdown += f"""

---

## Legalitas
- **SK Penetapan:** {safe(legalitas.get("sk_penetapan"))}
- **Terakhir Update:** {safe(legalitas.get("last_update"))}
"""

    return markdown

def safe(value):
    if value in (None, "", "null"):
        return "-"
    return value
