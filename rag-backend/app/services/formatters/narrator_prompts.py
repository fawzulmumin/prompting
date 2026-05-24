"""Prompt builder functions untuk narrator_service.py.

Semua fungsi di sini hanya membangun string prompt/template — tidak ada I/O,
tidak ada LLM call. Dipanggil oleh narrator_service.narrate_*() sebelum call LLM.
"""
from typing import Dict, Any, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# NUMERIC FILTER FORMATTER
# ─────────────────────────────────────────────────────────────────────────────

_NUMERIC_FIELD_LABELS = {
    "siswa":                  "siswa",
    "siswa_total":            "siswa",
    "siswa_l":                "siswa laki-laki",
    "siswa_p":                "siswa perempuan",
    "guru":                   "guru",
    "guru_total":             "guru",
    "guru_pns":               "guru PNS/ASN",
    "guru_honor":             "guru honorer",
    "guru_p3k":               "guru PPPK",
    "guru_sertifikasi":       "guru bersertifikasi",
    "guru_s1_keatas":         "guru S1 ke atas",
    "ruang_kelas":            "ruang kelas",
    "toilet_siswa_laki":      "toilet siswa laki-laki",
    "toilet_siswa_perempuan": "toilet siswa perempuan",
    "tendik":                 "tenaga kependidikan",
    "nilai_akhir":            "nilai akreditasi",
}

_OPERATOR_LABELS = {
    "gt":  "lebih dari",
    "gte": "minimal",
    "lt":  "kurang dari",
    "lte": "maksimal",
}


def format_numeric_filter_text(numeric_filters) -> str:
    """
    Konversi list NumericFilter ke deskripsi Bahasa Indonesia.
    Handle input None, list kosong, dataclass, maupun dict.

    Contoh output:
    - [gt siswa 300]            → "dengan lebih dari 300 siswa"
    - [gte guru_pns 5]          → "dengan minimal 5 guru PNS/ASN"
    - [between siswa 100 500]   → "dengan jumlah siswa antara 100 hingga 500"
    - []                        → ""
    """
    if not numeric_filters:
        return ""

    from app.services.core.es_query_builder import _NUMERIC_FIELD_MAP

    parts = []
    for f in numeric_filters:
        if hasattr(f, 'field'):
            field   = f.field
            op      = f.operator
            value   = f.value
            v_min   = f.value_min
            v_max   = f.value_max
        else:
            field   = f.get('field', '')
            op      = f.get('operator', '')
            value   = f.get('value')
            v_min   = f.get('value_min')
            v_max   = f.get('value_max')

        es_field = _NUMERIC_FIELD_MAP.get(field, field)
        label = _NUMERIC_FIELD_LABELS.get(es_field, _NUMERIC_FIELD_LABELS.get(field, field))

        if op == "between" and v_min is not None and v_max is not None:
            parts.append(f"dengan jumlah {label} antara {int(v_min)} hingga {int(v_max)}")
        elif op in _OPERATOR_LABELS and value is not None:
            val_str = str(int(value)) if isinstance(value, float) and value == int(value) else str(value)
            parts.append(f"dengan {_OPERATOR_LABELS[op]} {val_str} {label}")

    return ", ".join(parts) if parts else ""


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM BASE PROMPT
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_BASE = (
    "Kamu adalah Sistem Informasi Data Pendidikan resmi Kemendikdasmen "
    "(Kementerian Pendidikan Dasar dan Menengah) yang bertugas menyajikan "
    "data operasional satuan pendidikan di Indonesia.\n\n"

    "GAYA KOMUNIKASI:\n"
    "- Gunakan bahasa Indonesia baku sesuai PUEBI, formal namun mudah dipahami.\n"
    "- Awali setiap respons dengan kalimat pengantar yang memberikan konteks "
    "  wilayah, jenjang, atau topik yang ditanyakan.\n"
    "- Gunakan kata 'satuan pendidikan' atau 'sekolah', bukan 'lembaga' atau 'instansi'.\n"
    "- Format angka dengan titik sebagai pemisah ribuan: 1.234, bukan 1234 atau 1,234.\n"
    "- Hindari kata informal: 'yuk', 'oke', 'guys', 'so', 'well', 'basically'.\n"
    "- Akhiri respons dengan kalimat penutup yang mengundang pertanyaan lanjutan.\n\n"

    "FIELD KHUSUS INFRASTRUKTUR:\n"
    "- tipe_jamban: data sudah berupa label teks (mis. 'Leher Angsa', 'Tangki Septik'). Gunakan langsung.\n"
    "- kecukupan_air: data sudah berupa label teks (mis. 'Cukup', 'Tidak Cukup'). Gunakan langsung.\n"
    "- minum_siswa: jumlah unit air minum yang tersedia untuk siswa.\n"
    "- Jika field bernilai null, '-', atau 'data tidak tersedia', nyatakan 'data tidak tersedia'.\n"

    "PENANGANAN DATA:\n"
    "- Hanya bahas data yang tersedia dalam konteks yang diberikan.\n"
    "- Sertakan angka PERSIS seperti yang diberikan — dilarang membulatkan atau mengubah.\n"
    "- Jika breakdown jenjang tersedia, sebutkan komposisi 2-3 jenjang teratas.\n"
    "- Jika ada dua section data (utama + pendukung), integrasikan keduanya dalam narasi.\n"
    "- Jangan menyebutkan nama sistem, model AI, atau teknologi yang digunakan.\n\n"

    "FORMAT OUTPUT:\n"
    "- Tulis dalam paragraf, bukan poin-poin (kecuali diminta atau untuk daftar nama).\n"
    "- Jika ada data tabular, sajikan dalam tabel Markdown sebelum analisis.\n"
    "- Gunakan heading ### hanya jika ada 2+ sub-topik yang perlu dibedakan.\n"
    "- Panjang respons: 200-400 kata untuk query sederhana, "
    "  400-700 kata untuk query multi-modul, QC data, atau kompleks.\n\n"

    "LARANGAN KERAS:\n"
    "- Dilarang menambahkan angka, nama sekolah, atau fakta baru di luar konteks.\n"
    "- Dilarang menebak atau mengasumsikan data yang tidak tersedia.\n"
    "- Dilarang menjawab pertanyaan di luar domain satuan pendidikan.\n"
    "- Dilarang menyebut 'berdasarkan pelatihan saya' atau 'menurut pengetahuan saya'."
)

_BENCHMARK_DESA     = {"rendah": 5,    "sedang": 20,   "tinggi": 40}
_BENCHMARK_KEC      = {"rendah": 30,   "sedang": 100,  "tinggi": 300}
_BENCHMARK_KAB      = {"rendah": 500,  "sedang": 2000, "tinggi": 6000}
_BENCHMARK_PROVINSI = {"rendah": 5000, "sedang": 20000,"tinggi": 60000}

def _benchmark_label(total: int, level: str) -> str:
    tbl = {
        "desa_kelurahan": _BENCHMARK_DESA,
        "kecamatan":      _BENCHMARK_KEC,
        "kabupaten":      _BENCHMARK_KAB,
        "provinsi":       _BENCHMARK_PROVINSI,
    }.get(level, _BENCHMARK_DESA)
    if total <= tbl["rendah"]:
        return "tergolong sedikit untuk level wilayah ini"
    if total <= tbl["sedang"]:
        return "tergolong cukup memadai untuk level wilayah ini"
    return "tergolong banyak dan mencerminkan layanan pendidikan yang luas"


# ─────────────────────────────────────────────────────────────────────────────
# SHARED HELPERS (hanya dipakai oleh prompt builders di modul ini)
# ─────────────────────────────────────────────────────────────────────────────

def _safe(val, default: str = "-") -> str:
    """Konversi None/falsy/string-'None' ke default, hasil selalu str."""
    if val is None or str(val).strip().lower() == "none" or str(val).strip() == "":
        return default
    return str(val).strip()


def _build_wilayah_display_name(wilayah: str, wilayah_level: str) -> tuple:
    """
    Bangun nama tampilan wilayah yang jelas membedakan Kab vs Kota.

    Returns:
        (display_name: str, is_ambiguous: bool)
        is_ambiguous=True jika level=kabupaten tapi tidak ada prefix kota/kab.
    """
    w_lower = wilayah.lower().strip()
    if w_lower.startswith("kota "):
        return wilayah.title(), False
    elif w_lower.startswith(("kabupaten ", "kab. ", "kab ")):
        return wilayah.title(), False
    elif wilayah_level == "kabupaten":
        return f"Kabupaten/Kota {wilayah.title()}", True
    elif wilayah_level == "provinsi":
        return f"Provinsi {wilayah.title()}", False
    elif wilayah_level == "kecamatan":
        return f"Kecamatan {wilayah.title()}", False
    else:
        return wilayah.title(), False


def _build_school_table(schools: list, max_rows: int = 20) -> str:
    """
    Bangun tabel markdown dari daftar sekolah.
    Kolom: No | Nama | Jenjang | Akreditasi | Siswa | Kecamatan
    """
    if not schools:
        return ""

    header = "| No | Nama Sekolah | Jenjang | Akreditasi | Siswa | Kecamatan |\n"
    sep    = "|----|-------------|---------|-----------|-------|----------|\n"
    rows   = []
    for i, s in enumerate(schools[:max_rows], 1):
        nama     = _safe(s.get('nama'))
        jenjang  = _safe(s.get('bentuk_pendidikan') or s.get('bentuk'))
        akred    = _safe(s.get('akreditasi'))
        siswa    = _safe(s.get('siswa_total'))
        kec      = _safe(s.get('kecamatan'))
        rows.append(f"| {i} | {nama} | {jenjang} | {akred} | {siswa} | {kec} |")

    return header + sep + "\n".join(rows)


def _extract_profile_fields(profile_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalisasi profile_data ke field flat untuk prompt narrator.

    Struktur dari profile_builder.py::build_profile() (lihat es_structure.md):
      identitas.{nama, npsn, status, akreditasi, bentuk_pendidikan, nilai_akhir}
      lokasi.{alamat, kecamatan, kabupaten, provinsi, kode_pos}
      tenaga_pendidik.{total_guru, laki_laki, perempuan, guru_pns, guru_honor, guru_sertifikasi}
      tenaga_kependidikan.{total_tendik, laki_laki, perempuan}
      peserta_didik.{total_siswa, laki_laki, perempuan}
      sarana_prasarana.{akses_internet, sumber_listrik, sumber_air}
    """
    def _get(keys: list, default=None):
        for k in keys:
            if "." in k:
                parts = k.split(".", 1)
                sub = profile_data.get(parts[0], {})
                v = sub.get(parts[1]) if isinstance(sub, dict) else None
            else:
                v = profile_data.get(k)
            if v is not None and v != "" and v != "-":
                return v
        return default

    def _int(keys: list) -> int:
        v = _get(keys, 0)
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return 0

    def _float_or_none(keys: list):
        v = _get(keys)
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    # ── Guru ──────────────────────────────────────────────────────────────────
    # Prioritas: guru.* (profile_builder nested) → tenaga_pendidik.* (legacy) → flat
    guru_total = _int(["guru.total",      "tenaga_pendidik.total_guru", "jumlah_ptk", "total_ptk", "total_guru"])
    guru_l     = _int(["guru.laki_laki",  "tenaga_pendidik.laki_laki",  "guru_l"])
    guru_p     = _int(["guru.perempuan",  "tenaga_pendidik.perempuan",  "guru_p"])
    guru_pns   = _int(["guru.pns",        "tenaga_pendidik.guru_pns",   "guru_pns"])
    guru_honor = _int(["guru.honor",      "tenaga_pendidik.guru_honor", "guru_honor"])
    guru_serti = _int(["guru.sertifikasi","tenaga_pendidik.guru_sertifikasi", "guru_sertifikasi"])
    guru_s1    = _int(["guru.s1_keatas",  "guru_s1_keatas"])

    if guru_total == 0 and (guru_pns + guru_honor) > 0:
        guru_total = guru_pns + guru_honor

    # ── Tendik ────────────────────────────────────────────────────────────────
    tendik_total = _int(["tendik.total",    "tenaga_kependidikan.total_tendik", "tendik_total"])
    tendik_l     = _int(["tendik.laki_laki","tenaga_kependidikan.laki_laki",    "tendik_l"])
    tendik_p     = _int(["tendik.perempuan","tenaga_kependidikan.perempuan",    "tendik_p"])
    if tendik_total == 0 and (tendik_l + tendik_p) > 0:
        tendik_total = tendik_l + tendik_p

    # ── Siswa ─────────────────────────────────────────────────────────────────
    siswa_total = _int(["siswa.total", "peserta_didik.total_siswa", "total_siswa", "jumlah_siswa"])
    siswa_l     = _int(["siswa.laki_laki", "peserta_didik.laki_laki",   "siswa_l"])
    siswa_p     = _int(["siswa.perempuan", "peserta_didik.perempuan",   "siswa_p"])

    return {
        "nama":       _get(["identitas.nama", "identitas.nama_sekolah", "nama"], "Tidak diketahui"),
        "npsn":       str(_get(["identitas.npsn", "npsn"], "-")),
        "bentuk":     _get(["identitas.bentuk_pendidikan", "bentuk_pendidikan"], "-"),
        "status":     _get(["identitas.status", "identitas.status_sekolah", "status_sekolah"], "-"),
        "akreditasi": _get(["identitas.akreditasi", "akreditasi"], "-"),
        "nilai_akhir": _float_or_none(["identitas.nilai_akhir", "nilai_akhir"]),
        "alamat":     _get(["lokasi.alamat", "lokasi.alamat_jalan", "alamat_jalan"], ""),
        "kec":        _get(["lokasi.kecamatan",  "kecamatan"], ""),
        "kab":        _get(["lokasi.kabupaten",  "lokasi.kabupaten_kota", "kabupaten"], ""),
        "prov":       _get(["lokasi.provinsi",   "provinsi"], ""),
        "kode_pos":   _get(["lokasi.kode_pos",   "kode_pos"], "-"),
        "guru_total": guru_total,
        "guru_l":     guru_l,
        "guru_p":     guru_p,
        "guru_pns":   guru_pns,
        "guru_honor": guru_honor,
        "guru_serti": guru_serti,
        "guru_s1_keatas": guru_s1,
        "tendik_total": tendik_total,
        "tendik_l":     tendik_l,
        "tendik_p":     tendik_p,
        "siswa":      siswa_total,
        "siswa_l":    siswa_l,
        "siswa_p":    siswa_p,
        "internet":   _get(["sarana_prasarana.akses_internet", "akses_internet"], "-"),
        "listrik":    _get(["sarana_prasarana.sumber_listrik",  "sumber_listrik"], "-"),
        "air":        _get(["sarana_prasarana.sumber_air",      "sumber_air"], "-"),
        "tipe_jamban":   _get(["sarana_prasarana.tipe_jamban",  "tipe_jamban"], "-"),
        "kecukupan_air": _get(["sarana_prasarana.kecukupan_air","kecukupan_air"], "-"),
        "minum_siswa":   _get(["sarana_prasarana.minum_siswa",  "minum_siswa"], 0),
        "toilet_l":      _get(["sarana_prasarana.toilet_siswa_laki",    "toilet_siswa_laki"], 0),
        "toilet_p":      _get(["sarana_prasarana.toilet_siswa_perempuan","toilet_siswa_perempuan"], 0),
        "total_komputer":_get(["sarana_prasarana.total_komputer","total_komputer"], 0),
        "tahun_data":    _get(["identitas.tahun_data", "tahun"], "-"),
    }


QC_CONTEXT_LIBRARY: Dict[str, Dict[str, str]] = {
    "qc_1": {
        "nama": "NPSN unik",
        "penyebab_umum": "NPSN terdaftar lebih dari satu kali di sistem Dapodik atau duplikasi entri saat migrasi data.",
        "dampak": "Menyebabkan data sekolah ganda dan laporan agregat tidak akurat.",
        "rekomendasi": "Laporkan ke operator Dapodik untuk deduplication atau merge data.",
    },
    "qc_2": {
        "nama": "format nama sekolah",
        "penyebab_umum": "Nama sekolah mengandung karakter tidak valid, singkatan tidak baku, atau format berbeda dari standar (misal: huruf kecil semua, tanda baca berlebih).",
        "dampak": "Mengganggu pencarian dan pencocokan otomatis nama sekolah.",
        "rekomendasi": "Perbaiki nama sekolah sesuai format standar Dapodik (huruf kapital di awal setiap kata).",
    },
    "qc_3": {
        "nama": "nama sekolah tidak kosong",
        "penyebab_umum": "Field nama sekolah dikosongkan atau hanya berisi spasi saat entry data.",
        "dampak": "Sekolah tidak dapat ditemukan melalui pencarian nama.",
        "rekomendasi": "Isi nama resmi sekolah sesuai SK pendirian.",
    },
    "qc_4": {
        "nama": "alamat jalan",
        "penyebab_umum": "Alamat jalan tidak diisi, diisi generik ('jl tidak diketahui'), atau terlalu singkat.",
        "dampak": "Layanan berbasis lokasi (pengiriman surat dinas, verifikasi lapangan) terhambat.",
        "rekomendasi": "Isi alamat lengkap termasuk nama jalan, nomor, dan RT/RW.",
    },
    "qc_5": {
        "nama": "kode wilayah",
        "penyebab_umum": "Kode wilayah (kecamatan/kabupaten) tidak sesuai referensi Kemendikbud atau belum diperbarui saat pemekaran daerah.",
        "dampak": "Data tidak masuk agregasi wilayah yang benar; laporan per kecamatan/kabupaten menjadi tidak akurat.",
        "rekomendasi": "Perbarui kode wilayah menggunakan referensi terbaru dari Kemendikbud/BPS.",
    },
    "qc_6": {
        "nama": "koordinat lintang",
        "penyebab_umum": "Koordinat lintang (latitude) tidak diisi, bernilai 0, atau berada di luar batas wilayah Indonesia (-11° hingga 6°).",
        "dampak": "Sekolah tidak muncul pada peta, layanan geo-search tidak dapat menemukan sekolah terdekat.",
        "rekomendasi": "Perbarui koordinat menggunakan GPS ponsel atau Google Maps di lokasi sekolah.",
    },
    "qc_7": {
        "nama": "koordinat bujur",
        "penyebab_umum": "Koordinat bujur (longitude) tidak diisi, bernilai 0, atau di luar batas wilayah Indonesia (95° hingga 141°).",
        "dampak": "Sama dengan qc_6 — sekolah tidak terlokasi di peta.",
        "rekomendasi": "Perbarui koordinat bersamaan dengan qc_6.",
    },
    "qc_8": {
        "nama": "SK izin operasional",
        "penyebab_umum": "Nomor SK izin operasional tidak diisi atau dokumen belum diunggah ke Dapodik.",
        "dampak": "Legalitas operasional sekolah tidak terdokumentasi; berpotensi masalah saat audit atau akreditasi.",
        "rekomendasi": "Unggah dokumen SK izin operasional dari Dinas Pendidikan setempat.",
    },
    "qc_9": {
        "nama": "tanggal SK izin operasional",
        "penyebab_umum": "Tanggal SK izin operasional tidak diisi atau formatnya tidak valid.",
        "dampak": "Tidak dapat menghitung masa berlaku izin operasional secara otomatis.",
        "rekomendasi": "Isi tanggal dalam format DD-MM-YYYY sesuai dokumen SK.",
    },
    "qc_10": {
        "nama": "SK pendirian sekolah",
        "penyebab_umum": "Nomor SK pendirian tidak diisi; umum pada sekolah yang berdiri sebelum era digitalisasi.",
        "dampak": "Sejarah legal sekolah tidak terdokumentasi; diperlukan saat perpanjangan akreditasi.",
        "rekomendasi": "Cari arsip SK pendirian di Dinas Pendidikan atau arsip yayasan, lalu unggah.",
    },
    "qc_11": {
        "nama": "tanggal SK pendirian",
        "penyebab_umum": "Tanggal SK pendirian tidak diisi atau hanya berisi tahun tanpa tanggal/bulan.",
        "dampak": "Perhitungan usia sekolah tidak akurat.",
        "rekomendasi": "Isi tanggal lengkap dari dokumen SK pendirian.",
    },
    "qc_12": {
        "nama": "foto sekolah",
        "penyebab_umum": "Foto tampak depan sekolah belum diunggah ke Dapodik, atau file foto rusak/format tidak didukung.",
        "dampak": "Profil sekolah di portal resmi tidak memiliki gambar; mengurangi kepercayaan publik.",
        "rekomendasi": "Unggah foto tampak depan sekolah resolusi minimal 640×480 piksel.",
    },
    "qc_13": {
        "nama": "data yayasan",
        "penyebab_umum": "Informasi yayasan pengelola tidak diisi; wajib untuk sekolah swasta.",
        "dampak": "Status kepemilikan tidak dapat diverifikasi; berpotensi masalah legalitas sekolah swasta.",
        "rekomendasi": "Isi nama yayasan, nomor akta notaris, dan kontak yayasan di Dapodik.",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT BUILDERS  (satu fungsi per query-type)
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_aggregation(
    wilayah: str,
    total: int,
    level: str = "desa_kelurahan",
    status_filter: Optional[str] = None,
    breakdown_jenjang: Optional[Dict[str, int]] = None,
    breakdown_status: Optional[Dict[str, int]] = None,
    drilldown_top3: Optional[List[Dict[str, Any]]] = None,
    drilldown_field: Optional[str] = None,
    total_all: Optional[int] = None,
    pct_of_total: Optional[float] = None,
    active_filters: Optional[Dict] = None,
    jenjang_requested: Optional[List[str]] = None,
    metric_target: Optional[str] = None,
    metric_label: str = "satuan pendidikan",
    distribusi: Optional[list] = None,
    distribusi_field: Optional[str] = None,
    qc_detail: Optional[Dict] = None,
    numeric_filters: Optional[list] = None,
) -> str:
    status_info   = f" ({status_filter})" if status_filter else ""
    konteks_bench = _benchmark_label(total, level)
    level_label   = {
        "desa_kelurahan": "desa/kelurahan",
        "kecamatan":      "kecamatan",
        "kabupaten":      "kabupaten/kota",
        "provinsi":       "provinsi",
        "nasional":       "nasional",
    }.get(level, level)

    # ── Breakdown jenjang ────────────────────────────────────────────────────
    breakdown_section = ""
    if breakdown_jenjang:
        top_jenjang = sorted(
            breakdown_jenjang.items(), key=lambda x: -x[1]
        )[:6]
        jenjang_lines = ", ".join(f"{k}: {v:,}" for k, v in top_jenjang)
        breakdown_section += f"\n- Breakdown jenjang (top 6): {jenjang_lines}"

    # ── Breakdown status ─────────────────────────────────────────────────────
    if breakdown_status:
        # Gabungkan SWASTA dan Swasta (inkonsistensi data ES)
        status_clean = {}
        for k, v in breakdown_status.items():
            key = k.upper() if k.lower() in ("swasta", "negeri") else k
            status_clean[key] = status_clean.get(key, 0) + v

        negeri_ct = status_clean.get("NEGERI", 0)
        swasta_ct = status_clean.get("SWASTA", 0)
        total_ns  = negeri_ct + swasta_ct
        pct_sw    = round(swasta_ct / total_ns * 100) if total_ns else 0
        pct_ng    = (100 - pct_sw) if total_ns else 0
        breakdown_section += (
            f"\n- Breakdown status:"
            f" NEGERI: {negeri_ct:,} ({pct_ng}%),"
            f" SWASTA: {swasta_ct:,} ({pct_sw}%)"
        )

    # ── Drilldown top N ──────────────────────────────────────────────────────
    drilldown_section = ""
    if drilldown_top3 and drilldown_field:
        drilldown_label = {
            "kabupaten": "Kabupaten/Kota",
            "kecamatan": "Kecamatan",
            "desa_kelurahan": "Desa/Kelurahan",
        }.get(drilldown_field, drilldown_field.title())

        n_drilldown = len(drilldown_top3)
        drilldown_lines = "\n".join(
            f"  {i+1}. {item['name'].title()}: {item['count']:,} {metric_label}"
            for i, item in enumerate(drilldown_top3)
        )
        drilldown_section = (
            f"\n- Top {n_drilldown} {drilldown_label} terbanyak:\n{drilldown_lines}"
        )

    # ── QC detail: blok terpisah agar LLM tidak mengabaikan data kualitas ───
    qc_data_block = ""
    if qc_detail and qc_detail.get("top_masalah"):
        _total_brm = qc_detail.get("total_bermasalah", 0)
        _avg_brm   = qc_detail.get("avg_masalah", 0)
        _top_lines = "\n".join(
            f"  - {label}: {count:,} sekolah".replace(",", ".")
            for count, label in qc_detail["top_masalah"]
        )
        qc_data_block = (
            f"\nDATA KUALITAS (WAJIB DISEBUTKAN DI NARASI):\n"
            f"- Sekolah bermasalah (data tidak lengkap): {_total_brm:,}\n"
            f"- Rata-rata kolom masalah per sekolah: {_avg_brm}\n"
            f"- Kolom masalah terbanyak:\n{_top_lines}\n"
        )

    has_distribusi = bool(distribusi and distribusi_field)
    has_breakdown = bool(breakdown_section or drilldown_section or qc_data_block or has_distribusi)

    # ── Instruksi narasi ─────────────────────────────────────────────────────
    if has_breakdown:
        drilldown_instruksi = ""
        if drilldown_section:
            drilldown_instruksi = (
                "\n\n## Distribusi Wilayah\n"
                f"Sebutkan semua {n_drilldown} {drilldown_label} dengan jumlah {metric_label} "
                "terbanyak beserta angkanya — gunakan PERSIS data yang disuplai."
            )

        pct_instruksi = ""
        if total_all and pct_of_total is not None:
            pct_instruksi = (
                f"\n\n## Persentase\n"
                f"Sebutkan bahwa {total:,} sekolah ({pct_of_total}%) dari total "
                f"{total_all:,} sekolah di wilayah ini memenuhi kriteria filter."
            )

        qc_instruksi = ""
        if qc_data_block:
            n_qc = len(qc_detail.get("top_masalah", []))
            qc_instruksi = (
                f"\n\n## Detail Kualitas Data\n"
                f"WAJIB sebutkan {n_qc} kolom masalah terbanyak dengan jumlah sekolahnya "
                f"— gunakan PERSIS angka dari DATA KUALITAS di atas. "
                f"Format contoh: 'Kolom dengan masalah terbanyak adalah X ({'{count:,}'} sekolah), "
                f"diikuti Y dan Z.'"
            )

        distribusi_instruksi = ""
        if has_distribusi:
            _dl = {
                "sumber_air":            "Sumber Air",
                "waktu_penyelenggaraan": "Waktu Belajar",
                "status_kepemilikan":    "Status Kepemilikan",
                "partisipasi_bos":       "Partisipasi BOS",
                "akreditasi":            "Akreditasi",
            }.get(distribusi_field, distribusi_field.replace("_", " ").title())
            distribusi_instruksi = (
                f"\n\n## Distribusi {_dl}\n"
                f"WAJIB sebutkan distribusi {_dl} per kategori dengan angka dan persentasenya "
                f"— gunakan PERSIS data distribusi dari DATA AGREGASI di atas. "
                f"Ini adalah informasi utama yang diminta user."
            )

        # ── Instruksi komposisi jenjang (aware filter user) ────────────────
        if jenjang_requested:
            jenjang_instruksi = (
                f"## Komposisi Jenjang yang Diminta ({', '.join(jenjang_requested)})\n"
                "HANYA bahas jenjang yang disebutkan oleh user.\n"
                "JANGAN menyebut atau menampilkan jenjang lain yang tidak diminta "
                "(misalnya TK, KB, PAUD, dll).\n"
                "LARANGAN KERAS: Jika jenjang tidak diminta user, jangan disebutkan sama sekali.\n"
                "Sebutkan jumlah setiap jenjang yang diminta secara eksplisit "
                "— gunakan PERSIS angka dari breakdown di atas."
            )
        else:
            jenjang_instruksi = (
                "## Komposisi Jenjang\n"
                "Interpretasikan breakdown jenjang — jenjang apa yang dominan "
                "dan apa implikasinya terhadap layanan pendidikan."
            )

        instruksi = (
            "INSTRUKSI NARASI (gunakan ## sebagai heading per bagian):\n\n"
            "## Gambaran Umum\n"
            "Sebutkan wilayah, filter aktif, jumlah total yang memenuhi filter, "
            "dan apakah tergolong banyak/sedang/sedikit.\n\n"
            + jenjang_instruksi + "\n\n"
            "## Komposisi Status\n"
            "Sebutkan angka PERSIS NEGERI dan SWASTA beserta persentase masing-masing. "
            "Format: 'Negeri: X (Y%), Swasta: A (B%)'. "
            "Angka dan persentase harus PERSIS dari data di atas — jangan hitung ulang."
            + pct_instruksi
            + drilldown_instruksi
            + qc_instruksi
            + distribusi_instruksi +
            "\n\n## Implikasi\n"
            "Satu kalimat nilai praktis untuk perencanaan pendidikan.\n\n"
            "LARANGAN (jangan lakukan ini):\n"
            "- JANGAN tulis bagian dengan header \"Implikasi\", \"Rekomendasi Kebijakan\", "
            "\"Saran\", atau sejenisnya.\n"
            "- JANGAN tulis analisis makro tentang dampak kebijakan pemerintah daerah, "
            "ketimpangan pendidikan, atau rekomendasi strategis.\n"
            "- JANGAN menggunakan frasa seperti \"Data ini dapat menjadi acuan bagi pemerintah\", "
            "\"diperlukan intervensi\", atau sejenisnya.\n"
            "- JANGAN menyisipkan analisis atau opini di dalam paragraf komposisi, "
            "misalnya kalimat seperti: "
            "\"yang mungkin memerlukan perhatian khusus\", "
            "\"yang perlu mendapat perhatian pemerintah\", "
            "\"menunjukkan ketimpangan yang perlu diatasi\", "
            "\"mengindikasikan perlunya intervensi\".\n"
            "- Setiap paragraf harus berisi FAKTA DAN ANGKA SAJA dari data yang disuplai. "
            "Tidak boleh ada interpretasi makro atau rekomendasi tersembunyi di dalam paragraf data.\n"
            "- Boleh menulis 1 kalimat penutup yang mengundang pertanyaan lanjutan dari user, "
            "tapi isinya harus terbatas pada data yang sudah disuplai (jenjang, status, wilayah, dsb)."
        )
    else:
        _nf_instruksi = ""
        if numeric_filters:
            _nf_desc = format_numeric_filter_text(numeric_filters)
            if _nf_desc:
                _nf_instruksi = (
                    f"\nPERHATIAN FILTER: Angka {total:,} adalah hasil filter ({_nf_desc}). "
                    f"Kalimat 1 WAJIB menyebutkan kondisi filter ini, "
                    f"contoh: 'Terdapat {total:,} {metric_label} di {wilayah} yang {_nf_desc[7:] if _nf_desc.startswith('dengan ') else _nf_desc}.' "
                    f"JANGAN gunakan kata 'tergolong sedikit/banyak' untuk angka yang merupakan hasil filter.\n"
                )
        instruksi = (
            "INSTRUKSI NARASI (3 kalimat tepat, TANPA bullet point, TANPA heading):\n"
            f"Kalimat 1: Sebutkan PERSIS angka {total:,} {metric_label} di {wilayah} "
            f"({level_label}). Jangan bulatkan atau ubah angka ini.\n"
            "Kalimat 2: Bandingkan dengan benchmark — gunakan label konteks yang "
            "sudah disediakan (banyak/sedang/sedikit).\n"
            "Kalimat 3: Satu implikasi praktis bagi perencanaan pendidikan di wilayah ini.\n"
            f"LARANGAN KERAS: Jangan tulis angka selain {total:,} untuk jumlah {metric_label}.\n"
            + _nf_instruksi + "\n"
            "LARANGAN (jangan lakukan ini):\n"
            "- JANGAN tulis bagian dengan header \"Implikasi\", \"Rekomendasi Kebijakan\", "
            "\"Saran\", atau sejenisnya.\n"
            "- JANGAN tulis analisis makro tentang dampak kebijakan pemerintah daerah, "
            "ketimpangan pendidikan, atau rekomendasi strategis.\n"
            "- JANGAN menggunakan frasa seperti \"Data ini dapat menjadi acuan bagi pemerintah\", "
            "\"diperlukan intervensi\", atau sejenisnya.\n"
            "- JANGAN menyisipkan analisis atau opini di dalam paragraf komposisi, "
            "misalnya kalimat seperti: "
            "\"yang mungkin memerlukan perhatian khusus\", "
            "\"yang perlu mendapat perhatian pemerintah\", "
            "\"menunjukkan ketimpangan yang perlu diatasi\", "
            "\"mengindikasikan perlunya intervensi\".\n"
            "- Setiap paragraf harus berisi FAKTA DAN ANGKA SAJA dari data yang disuplai. "
            "Tidak boleh ada interpretasi makro atau rekomendasi tersembunyi di dalam paragraf data.\n"
            "- Boleh menulis 1 kalimat penutup yang mengundang pertanyaan lanjutan dari user, "
            "tapi isinya harus terbatas pada data yang sudah disuplai (jenjang, status, wilayah, dsb)."
        )

    # ── Filter aktif & persentase ────────────────────────────────────────────
    filter_section = ""
    _filter_parts = []
    if active_filters:
        if active_filters.get("bentuk_pendidikan"):
            _filter_parts.append(f"jenjang {active_filters['bentuk_pendidikan']}")
        if active_filters.get("status_sekolah"):
            _filter_parts.append(f"status {active_filters['status_sekolah']}")
        if active_filters.get("akreditasi_gte"):
            grades = "/".join(active_filters["akreditasi_gte"])
            _filter_parts.append(f"akreditasi {grades}")
        elif active_filters.get("akreditasi_exact"):
            _filter_parts.append(f"akreditasi {active_filters['akreditasi_exact']}")
    numeric_filter_desc = format_numeric_filter_text(numeric_filters or [])
    if numeric_filter_desc:
        _filter_parts.append(numeric_filter_desc)
    if _filter_parts:
        filter_section = f"\n- Filter aktif: {', '.join(_filter_parts)}"

    pct_section = ""
    if total_all and pct_of_total is not None:
        pct_section = (
            f"\n- Total SEMUA sekolah di wilayah (tanpa filter): {total_all:,}"
            f"\n- Persentase yang memenuhi filter: {pct_of_total}%"
        )

    # ── Distribusi section ────────────────────────────────────────────────────
    distribusi_section = ""
    if distribusi and distribusi_field:
        _DIST_LABELS = {
            "sumber_air":            "Sumber Air",
            "waktu_penyelenggaraan": "Waktu Belajar",
            "status_kepemilikan":    "Status Kepemilikan",
            "partisipasi_bos":       "Partisipasi BOS",
            "akreditasi":            "Akreditasi",
        }
        dist_label = _DIST_LABELS.get(distribusi_field, distribusi_field.replace("_", " ").title())
        dist_lines = ", ".join(
            f"{item['key']}: {item['count']:,} ({item['pct']}%)"
            for item in distribusi[:8]
        )
        distribusi_section = (
            f"\n- Distribusi {dist_label}: {dist_lines}"
            f"\n  (Catatan: ceritakan distribusi ini sebagai informasi utama jawaban)"
        )

    # Label subjek dinamis: "sekolah" untuk count biasa, "siswa laki-laki" dll untuk metric
    _is_school_count = not metric_target or metric_target == "sekolah"
    subyek = "sekolah" if _is_school_count else metric_label

    # Tambahkan catatan override jika metric bukan sekolah, agar LLM tidak terganggu
    # oleh instruksi "gunakan kata sekolah" di _SYSTEM_BASE
    metric_override = (
        f"\nCATATAN: Topik narasi ini adalah {metric_label.upper()}, BUKAN sekolah. "
        f"Gunakan kata '{metric_label}' sebagai subjek utama, bukan 'sekolah'."
        if not _is_school_count else ""
    )

    return f"""{_SYSTEM_BASE}{metric_override}

DATA AGREGASI:
- Wilayah      : {wilayah} (level: {level_label})
- Metrik       : {metric_label}
- Jumlah {subyek}{status_info}: {total:,}{filter_section}{pct_section}
- Konteks      : jumlah ini {konteks_bench}{breakdown_section}{distribusi_section}{drilldown_section}
{qc_data_block}
{instruksi}

NARASI:"""


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT AGGREGATION MULTI-MODUL
# (untuk query yang menggabungkan aggregation + list atau aggregation + geo)
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_aggregation_with_list(
    wilayah: str,
    total: int,
    schools_list: list,
    *,
    jenjang: str = None,
    akreditasi: str = None,
    status: str = None,
    breakdown_jenjang: dict = None,
    metric_label: str = "satuan pendidikan",
    metric_target: str = None,
) -> str:
    """
    Prompt untuk query yang meminta aggregation DAN daftar sekolah sekaligus.
    Contoh: "berapa SMK akreditasi A di Bandung? sebutkan juga nama-namanya"
    """
    filter_desc = []
    if jenjang:
        filter_desc.append(f"jenjang {jenjang}")
    if status:
        filter_desc.append(f"berstatus {status}")
    if akreditasi:
        filter_desc.append(f"terakreditasi {akreditasi}")
    filter_str = " ".join(filter_desc) or "semua jenjang"
    # Label metric untuk semua metric_target yang didukung (lihat aggregation_core.py)
    _METRIC_LABELS = {
        "sekolah":          "sekolah",
        "siswa":            "siswa",
        "siswa_l":          "siswa laki-laki",
        "siswa_p":          "siswa perempuan",
        "siswa_baru":       "siswa baru",
        "siswa_mengulang":  "siswa mengulang",
        "siswa_lulus":      "siswa lulus",
        "guru":             "guru/PTK",
        "guru_pns":         "guru PNS/ASN",
        "guru_honor":       "guru honorer",
        "guru_sertifikasi": "guru bersertifikasi",
        "guru_s1":          "guru lulusan S1+",
        "guru_p3k":         "guru PPPK",
        "guru_l":           "guru laki-laki",
        "guru_p_gender":    "guru perempuan",
        "tendik":           "tenaga kependidikan",
        "total_komputer":   "komputer",
    }

    metric_label = _METRIC_LABELS.get(metric_target, metric_target or "sekolah") if metric_target else "sekolah"
    is_numeric_metric = metric_target and metric_target != "sekolah"
    school_lines = "\n".join(
        f"- {_safe(s.get('nama'), 'Nama tidak tersedia')} "
        f"({_safe(s.get('kecamatan'))})"
        for s in schools_list[:20]  # batasi 20 untuk context
    )

    breakdown_str = ""
    if breakdown_jenjang:
        top = sorted(breakdown_jenjang.items(), key=lambda x: -x[1])[:5]
        breakdown_str = "Breakdown jenjang: " + ", ".join(f"{k}: {v:,}" for k, v in top)
    note_str = (
        f"- Catatan: angka ini adalah JUMLAH {metric_label.upper()}, bukan jumlah sekolah.\n"
        if is_numeric_metric
        else ""
    )
    return (
        f"{_SYSTEM_BASE}\n\n"
        "TUGAS: Buat narasi yang menggabungkan dua informasi berikut:\n\n"
        f"DATA AGGREGASI:\n"
        f"- Wilayah: {wilayah}\n"
        f"- Filter: {filter_str}\n"
        f"- Total {metric_label}: {total:,}\n"
        f"{note_str}"
        f"- Jenis data: {'jumlah sekolah' if not metric_target or metric_target == 'sekolah' else 'jumlah ' + metric_label}\n"
        f"{breakdown_str}\n\n"
        f"DAFTAR SEKOLAH (sebagian):\n{school_lines}\n\n"
        "INSTRUKSI:\n"
        "1. Mulai dengan menyebutkan total sekolah secara jelas.\n"
        "2. Lanjutkan dengan analisis komposisi (jika breakdown tersedia).\n"
        "3. Di bagian akhir, sajikan daftar nama dalam format tabel atau poin.\n"
        "4. Gunakan kalimat transisi yang alami antar bagian.\n"
        "5. Tutup dengan ajakan klarifikasi jika data daftar lebih banyak dari yang ditampilkan.\n"
    )

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT OOC GUARDRAIL (versi baru, lebih konsisten tone Kemendikdasmen)
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_ooc_guardrail(query: str) -> str:
    return f"""{_SYSTEM_BASE}

Pengguna mengajukan pertanyaan berikut:
"{query}"

Pertanyaan tersebut berada di luar cakupan data yang tersedia dalam sistem ini.

INSTRUKSI:
1. Tolak dengan sopan dan profesional.
2. Jelaskan secara singkat bahwa sistem hanya menyediakan data satuan pendidikan.
3. Berikan 2–3 contoh pertanyaan yang dapat dijawab sistem.
4. Jangan mencoba menjawab pertanyaan di luar konteks.
5. Jangan menyebut AI, model, atau database.

FORMAT:
3–4 kalimat, bahasa formal.

Contoh pertanyaan yang sesuai:
- "Berapa jumlah SMA negeri di Jakarta?"
- "Daftar SMK akreditasi A di Surabaya"
- "Profil SMAN 3 Bandung"

NARASI:"""

def _prompt_comparison_lokasi(
    results: List[Dict[str, Any]],
    results_detail: Optional[List[Dict[str, Any]]] = None,
    query: str = "",
) -> str:
    """
    Prompt perbandingan lokasi yang adaptif berdasarkan intent query:
    - Query menyebut rata-rata/avg siswa  → fokus ke avg_siswa per sekolah
    - Query menyebut jenjang spesifik     → highlight jenjang itu saja
    - Default                             → perbandingan jumlah sekolah
    """
    import re as _re

    # ── Deteksi intent dari query ─────────────────────────────────────────────
    _wants_avg  = bool(_re.search(
        r'\brata.rata\b|\bavg\b|\bper sekolah\b|\bpadat\b|\bkepadatan\b', query, _re.I
    ))
    _jenjang_req = None
    for _j in ["SMA", "SMK", "SMP", "SD", "MI", "MTs", "MA", "TK", "PAUD", "SLB"]:
        if _re.search(rf'\b{_j}\b', query, _re.I):
            _jenjang_req = _j
            break

    # ── Data jumlah sekolah (selalu ada) ─────────────────────────────────────
    data_lines = "\n".join(
        f"- {r['lokasi']}: {r['count']:,} sekolah"
        for r in results
    )
    best    = max(results, key=lambda x: x["count"])
    worst   = min(results, key=lambda x: x["count"])
    selisih = best["count"] - worst["count"]
    pct     = round(selisih / worst["count"] * 100) if worst["count"] > 0 else 0
    dominan_label = (
        "perbedaan yang cukup signifikan" if pct >= 20
        else "perbedaan yang moderat"     if pct >= 10
        else "perbedaan yang relatif kecil"
    )

    # ── Seksi avg siswa (hanya jika query meminta + data tersedia) ───────────
    avg_section = ""
    if _wants_avg and results_detail:
        avg_rows = []
        for r in results_detail:
            avg = r.get("avg_siswa")
            if avg:
                avg_rows.append(
                    f"  {r['lokasi']}: rata-rata {round(avg):,} siswa/sekolah"
                    + (f" (maks {round(r['max_siswa']):,})" if r.get("max_siswa") else "")
                )
        if avg_rows:
            best_avg = max(
                (r for r in results_detail if r.get("avg_siswa")),
                key=lambda x: x.get("avg_siswa", 0),
                default=None,
            )
            avg_section = (
                "\n\nDATA RATA-RATA SISWA PER SEKOLAH"
                + (f" (jenjang {_jenjang_req})" if _jenjang_req else "")
                + ":\n" + "\n".join(avg_rows)
            )
            if best_avg:
                avg_section += (
                    f"\n- Wilayah lebih padat: {best_avg['lokasi']} "
                    f"(rata-rata {round(best_avg['avg_siswa']):,} siswa/sekolah)"
                )

    # ── Seksi breakdown per lokasi ────────────────────────────────────────────
    breakdown_section = ""
    if results_detail:
        breakdown_lines = []
        for r in results_detail:
            lokasi = r.get("lokasi") or r.get("value", "")
            bj = r.get("breakdown_jenjang", {})
            bs = r.get("breakdown_status", {})

            if bj:
                if _jenjang_req and _jenjang_req in bj:
                    # Tampilkan hanya jenjang yang diminta
                    breakdown_lines.append(
                        f"  {lokasi} — {_jenjang_req}: {bj[_jenjang_req]:,} sekolah"
                    )
                else:
                    top3 = sorted(bj.items(), key=lambda x: -x[1])[:3]
                    breakdown_lines.append(
                        f"  {lokasi} — jenjang dominan: "
                        + ", ".join(f"{k}: {v:,}" for k, v in top3)
                    )

            if bs:
                negeri   = bs.get("NEGERI", 0) + bs.get("Negeri", 0)
                swasta   = bs.get("SWASTA", 0) + bs.get("Swasta", 0)
                total_ns = negeri + swasta
                if total_ns > 0:
                    pct_sw = round(swasta / total_ns * 100)
                    breakdown_lines.append(
                        f"  {lokasi} — Negeri: {negeri:,} / Swasta: {swasta:,} ({pct_sw}% swasta)"
                    )

        if breakdown_lines:
            breakdown_section = "\n\nBREAKDOWN PER LOKASI:\n" + "\n".join(breakdown_lines)

    # ── Instruksi adaptif berdasarkan intent ─────────────────────────────────
    _tabel_instruksi = (
        "1. Tampilkan tabel perbandingan berikut WAJIB di awal respons:\n\n"
        "   | Metrik | [nama wilayah 1] | [nama wilayah 2] |\n"
        "   |--------|-----------------|------------------|\n"
        "   | Total Sekolah | [angka dari data] | [angka dari data] |\n"
        "   | Selisih Absolut | [hitung dari data] | - |\n"
        "   | Selisih Relatif | [hitung persen] | - |\n\n"
        "   Isi semua nilai dari DATA yang disuplai. JANGAN mengarang angka.\n"
    )

    if _wants_avg and avg_section:
        instruksi = (
            "INSTRUKSI NARASI:\n"
            + _tabel_instruksi +
            "2. JAWAB langsung: sebutkan rata-rata siswa/sekolah masing-masing wilayah dengan angka persis.\n"
            "3. Nyatakan wilayah mana yang lebih padat berdasarkan rata-rata — bukan jumlah sekolah.\n"
            f"4. Jika ada filter jenjang ({_jenjang_req or 'semua'}), sebutkan secara eksplisit.\n"
            "5. Tutup dengan 1 kalimat yang mengundang pertanyaan lanjutan.\n"
            "6. JANGAN tulis analisis implikasi kebijakan, opini, atau rekomendasi di luar data yang disuplai."
        )
    elif _jenjang_req:
        instruksi = (
            "INSTRUKSI NARASI:\n"
            + _tabel_instruksi +
            f"2. Fokus pada jenjang {_jenjang_req}: sebutkan jumlahnya di masing-masing wilayah.\n"
            "   JANGAN jadikan total sekolah semua jenjang sebagai jawaban utama.\n"
            "3. Jika ada breakdown jenjang atau status dari data: sebutkan dalam 1-2 kalimat tambahan.\n"
            "4. Tutup dengan 1 kalimat yang mengundang pertanyaan lanjutan.\n"
            "5. JANGAN tulis analisis implikasi kebijakan, opini, atau rekomendasi di luar data yang disuplai."
        )
    else:
        instruksi = (
            "INSTRUKSI NARASI:\n"
            + _tabel_instruksi +
            "2. Tulis paragraf analisis singkat (2-3 kalimat): sebutkan mana\n"
            "   yang lebih banyak/besar, selisih absolutnya berapa, dan\n"
            "   selisih relatifnya berapa persen.\n"
            "3. Jika ada breakdown jenjang atau status dari data: sebutkan\n"
            "   dalam 1-2 kalimat tambahan.\n"
            "4. Tutup dengan 1 kalimat yang mengundang pertanyaan lanjutan,\n"
            "   misalnya: rincian berdasarkan jenjang, status, atau kecamatan.\n"
            "5. JANGAN tulis analisis implikasi kebijakan, opini, atau\n"
            "   rekomendasi di luar data yang sudah disuplai."
        )

    return f"""{_SYSTEM_BASE}

PERTANYAAN USER: {query or "(tidak diketahui)"}

DATA PERBANDINGAN:
{data_lines}

FAKTA TURUNAN (sudah dihitung, gunakan persis):
- Wilayah terbanyak : {best['lokasi']} ({best['count']:,} sekolah)
- Selisih absolut   : {selisih:,} sekolah
- Selisih relatif   : {pct}% ({dominan_label}){avg_section}{breakdown_section}

{instruksi}

NARASI:"""

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT COMPARISON SEKOLAH VS SEKOLAH (perbandingan dua sekolah secara langsung)
# ─────────────────────────────────────────────────────────────────────────────
def _prompt_school_vs_school(school_a: dict, school_b: dict, query: str) -> str:
    """Prompt khusus perbandingan dua sekolah individual."""

    def fmt(s):
        return (
            f"Nama: {_safe(s.get('nama'))} | Jenjang: {_safe(s.get('bentuk_pendidikan'))} | "
            f"Status: {_safe(s.get('status_sekolah'))} | Akreditasi: {_safe(s.get('akreditasi'))} | "
            f"Kecamatan: {_safe(s.get('kecamatan'))} | Siswa: data tidak tersedia | "
            f"Guru: data tidak tersedia"
        )

    return f"""{_SYSTEM_BASE}

PERTANYAAN USER: {query}

DATA SEKOLAH A:
{fmt(school_a)}

DATA SEKOLAH B:
{fmt(school_b)}

INSTRUKSI (jawab PERSIS pertanyaan user, jangan keluar dari data di atas):
1. Bandingkan aspek yang ditanyakan secara langsung — sebutkan angka persis.
2. Sebutkan kecamatan masing-masing.
3. Nyatakan kesimpulan: mana yang lebih besar/lebih baik sesuai kriteria user.
LARANGAN: jangan tambahkan angka di luar data di atas.

NARASI:"""

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT COMPARISON + LIST
# ─────────────────────────────────────────────────────────────────────────────

def _prompt_comparison_with_list(
    results: list,
    schools_list: list,
    template_md: str,
    *,
    jenjang: str = None,
    akreditasi: str = None,
) -> str:

    filter_parts = []
    if jenjang:
        filter_parts.append(f"jenjang {jenjang}")
    if akreditasi:
        filter_parts.append(f"akreditasi {akreditasi}")

    filter_str = " dan ".join(filter_parts) or "semua jenjang"

    locations_summary = "\n".join(
        f"- {r['lokasi']}: {r['count']:,} sekolah"
        for r in results
    )

    sample_schools = "\n".join(
        f"- {s.get('nama','-')} ({s.get('bentuk_pendidikan','-')}, {s.get('kecamatan','-')})"
        for s in schools_list[:15]
    )

    return f"""{_SYSTEM_BASE}

TUGAS:
Buat narasi perbandingan antar wilayah dan sertakan contoh sekolah sebagai pendukung.

FILTER:
{filter_str}

DATA PERBANDINGAN:
{locations_summary}

CONTOH SEKOLAH:
{sample_schools}

INSTRUKSI:
1. Mulai dengan ringkasan wilayah dengan jumlah sekolah terbanyak.
2. Sebutkan selisih jumlah jika signifikan.
3. Transisikan secara natural ke contoh sekolah.
4. Sajikan daftar sekolah dalam bentuk tabel markdown.
5. Tutup dengan catatan bahwa daftar hanya sebagian data.

NARASI:"""

def _prompt_comparison_status(
    wilayah: str,
    negeri: int,
    swasta: int,
    pct_diff: int,
) -> str:
    dominan = "Negeri" if negeri > swasta else "Swasta"
    total   = negeri + swasta
    pct_negeri = round(negeri / total * 100) if total > 0 else 0
    pct_swasta = round(swasta / total * 100) if total > 0 else 0

    return f"""{_SYSTEM_BASE}

DATA KOMPOSISI SEKOLAH:
- Wilayah: {wilayah}
- Sekolah Negeri : {negeri:,} ({pct_negeri}%)
- Sekolah Swasta : {swasta:,} ({pct_swasta}%)
- Total          : {total:,} sekolah
- Dominasi       : {dominan} lebih banyak sebesar {abs(pct_diff)}%

INSTRUKSI NARASI:
1. Tampilkan tabel perbandingan berikut WAJIB di awal respons:

   | Metrik | Negeri | Swasta |
   |--------|--------|--------|
   | Jumlah Sekolah | [angka dari data] | [angka dari data] |
   | Persentase | [pct dari data]% | [pct dari data]% |
   | Selisih Absolut | [hitung dari data] | - |

   Isi semua nilai dari DATA yang disuplai. JANGAN mengarang angka.
2. Tulis paragraf analisis singkat (2-3 kalimat): sebutkan mana yang
   lebih banyak, selisih absolutnya berapa, dan persentasenya.
3. Jika ada breakdown jenjang atau konteks tambahan dari data: sebutkan
   dalam 1-2 kalimat tambahan.
4. Tutup dengan 1 kalimat yang mengundang pertanyaan lanjutan.
5. JANGAN tulis analisis implikasi kebijakan, opini, atau rekomendasi
   di luar data yang disuplai.

NARASI:"""


def _prompt_school_list(
    wilayah: str,
    total: int,
    ditampilkan: int,
    schools: List[Dict[str, Any]],
    breakdown_jenjang: Optional[Dict[str, int]] = None,
    breakdown_status: Optional[Dict[str, int]] = None,
    active_filters: Optional[Dict[str, str]] = None,
    wilayah_level: str = "",
    qc_summary: Optional[str] = None,
    desa_ambiguous: bool = False,
    desa_candidates: Optional[List[Dict[str, Any]]] = None,
    qc_note: Optional[str] = None,
) -> str:
    wilayah_full, _is_ambiguous = _build_wilayah_display_name(wilayah, wilayah_level)
    _nama_clean = wilayah.title()
    _ambiguity_disclaimer = (
        f"\n\n*Catatan: Pencarian ini mencakup Kabupaten {_nama_clean} dan "
        f"Kota {_nama_clean} sekaligus. "
        f"Gunakan prefix \"kota\" atau \"kabupaten\" untuk memfilter lebih spesifik.*"
    ) if _is_ambiguous else ""

    if desa_ambiguous and desa_candidates:
        _kand_str = "; ".join(
            f"{c.get('kecamatan','?')} ({c.get('kabupaten','?')})"
            for c in desa_candidates[:4]
        )
        _ambiguity_disclaimer += (
            f"\n\n*Catatan: Nama desa/kelurahan \"{_nama_clean}\" ditemukan di "
            f"beberapa wilayah: {_kand_str}. "
            f"Hasil mencakup semua lokasi tersebut. "
            f"Sebutkan kecamatan atau kabupaten untuk hasil yang lebih spesifik.*"
        )
    # Gunakan breakdown dari ES aggregation jika tersedia,
    # fallback ke hitung dari sampel
    if breakdown_jenjang:
        jenjang_str = ", ".join(
            f"{k}: {v:,}"
            for k, v in sorted(breakdown_jenjang.items(), key=lambda x: -x[1])[:5]
        ) + " (data lengkap dari seluruh radius)"
    else:
        bc: Dict[str, int] = {}
        for s in schools:
            b = s.get("bentuk", "")
            if b:
                bc[b] = bc.get(b, 0) + 1
        jenjang_str = ", ".join(
            f"{k}: {v}" for k, v in sorted(bc.items(), key=lambda x: -x[1])[:4]
        ) or "tidak tersedia"
        jenjang_str += " *(estimasi dari sampel yang ditampilkan)*"

    if breakdown_status:
        # Normalisasi: gabungkan variasi kapitalisasi
        _STATUS_NORMALIZE = {
            "negeri":      "Negeri",
            "swasta":      "Swasta",
            "luar negeri": "Luar Negeri",
            "alih bentuk": "Alih Bentuk",
            "tutup":       "Tutup",
            "lainnya":     "Lainnya",
        }
        _STATUS_NOISE = {"aktif", "aktif (buka)", "buka"}

        normalized_st: Dict[str, int] = {}
        for k, v in breakdown_status.items():
            k_lower = k.lower().strip()
            if k_lower in _STATUS_NOISE:
                continue
            canonical = _STATUS_NORMALIZE.get(k_lower, k.title())
            normalized_st[canonical] = normalized_st.get(canonical, 0) + v

        total_st = sum(normalized_st.values())
        status_parts = []
        for k, v in sorted(normalized_st.items(), key=lambda x: -x[1]):
            pct = f"{round(v / total_st * 100)}%" if total_st else "-"
            status_parts.append(f"{k}: {v:,} ({pct})")

        if status_parts:
            status_str = ", ".join(status_parts)
            status_str += " (data lengkap dari seluruh radius)"
        else:
            status_str = "tidak tersedia"
    else:
        sc_tmp: Dict[str, int] = {}
        for s in schools:
            st = s.get("status", "")
            if st:
                sc_tmp[st.capitalize()] = sc_tmp.get(st.capitalize(), 0) + 1
        status_str = ", ".join(
            f"{k}: {v}" for k, v in sorted(sc_tmp.items(), key=lambda x: -x[1])
        ) or "tidak tersedia"
        status_str += " *(estimasi dari sampel yang ditampilkan)*"

        # Bangun deskripsi filter aktif untuk narasi
    filter_parts = []
    if active_filters:
        jenjang_val = (
            active_filters.get("bentuk")
            or active_filters.get("bentuk_pendidikan")
        )
        if jenjang_val:
            filter_parts.append(f"jenjang {jenjang_val}")
        if active_filters.get("status"):
            filter_parts.append(f"status {active_filters['status']}")
        if active_filters.get("akreditasi"):
            filter_parts.append(f"akreditasi {active_filters['akreditasi']}")
        if active_filters.get("siswa_min"):
            filter_parts.append(f"jumlah siswa di atas {active_filters['siswa_min']} orang")
        if active_filters.get("siswa_max"):
            filter_parts.append(f"jumlah siswa di bawah {active_filters['siswa_max']} orang")
    filter_desc = ", ".join(filter_parts) if filter_parts else "semua jenjang dan status"
    # Bangun tabel sekolah jika ada data
    school_table = _build_school_table(schools)

    _qc_instruksi_str = ""
    if qc_summary:
        # Hitung jumlah sekolah dalam qc_summary untuk menentukan instruksi
        qc_lines = [ln for ln in qc_summary.strip().split("\n") if ln.strip().startswith("-")]
        total_qc_shown = len(qc_lines) if qc_lines else ditampilkan

        if total_qc_shown <= 10:
            contoh_instruksi = (
                "- Sebutkan SEMUA sekolah yang ditemukan beserta masalah spesifiknya\n"
                "- Format per sekolah: **Nama Sekolah** → [daftar masalah yang ditemukan]\n"
            )
        else:
            contoh_instruksi = (
                "- Sebutkan contoh spesifik 3-5 sekolah beserta masalahnya\n"
                "- Sebutkan masalah yang paling sering muncul di semua sekolah\n"
            )

        _qc_instruksi_str = (
            "\n\nINSTRUKSI KHUSUS — QUERY RESIDU DATA:\n"
            "User menanyakan sekolah yang datanya BERMASALAH. "
            "Fokus narasi pada masalah data yang ditemukan, bukan pada akreditasi atau jenjang.\n"
            "Sertakan dalam narasi:\n"
            "- Jumlah total sekolah bermasalah yang ditemukan\n"
            + contoh_instruksi +
            "- Untuk setiap sekolah: sebutkan nama, lokasi, dan maksimal 3 masalah utama. "
            "Jika lebih dari 3 masalah, tulis 'dan N masalah lainnya'.\n"
            "- Jangan ulangi masalah yang sama di banyak sekolah — fokus pada yang unik/parah.\n"
            "- Jangan sebutkan breakdown akreditasi kecuali diminta\n"
            "- Untuk setiap sekolah bermasalah: jelaskan MENGAPA data tersebut bermasalah, "
            "bukan hanya MENYEBUTKAN bahwa ada masalah.\n"
            "- Kelompokkan masalah jika ada pola serupa (mis. banyak sekolah tanpa koordinat GPS, "
            "atau banyak yang foto sekolah kosong).\n"
            "- Tulis rekomendasi 1 kalimat apa yang bisa dilakukan "
            "(mis. verifikasi lapangan, update data di Dapodik).\n"
            f"\nData masalah:\n{qc_summary}"
        )

    _qc_note_section = f"\n\n{qc_note}" if qc_note else ""
    _qc_note_instruksi = (
        "\n5b. Ada catatan QC di bawah tabel. Sebutkan setiap sekolah bermasalah "
        "dalam format: '(⚠️ [nama sekolah] memiliki X masalah data: [daftar masalah])'. "
        "Maksimal 3 sekolah. Jika tidak ada catatan QC, lewati poin ini."
        if qc_note else ""
    )

    return f"""{_SYSTEM_BASE}{_qc_instruksi_str}

DATA DAFTAR SEKOLAH:
- Wilayah         : {wilayah_full}
- Filter aktif    : {filter_desc}
- Total ditemukan : {total:,} sekolah
- Ditampilkan     : {ditampilkan} sekolah (halaman pertama)
- Komposisi jenjang (TOTAL): {jenjang_str}
- Komposisi status (TOTAL) : {status_str}

DAFTAR SEKOLAH:
{school_table}{_qc_note_section}

INSTRUKSI NARASI:
1. Tampilkan tabel daftar sekolah (dari DAFTAR SEKOLAH di atas) PERSIS seperti adanya — jangan ubah format, urutan, atau isi tabel.
2. Tulis paragraf pembuka (2-3 kalimat): sebutkan wilayah, filter aktif, total sekolah yang ditemukan, dan konteks apakah jumlah ini banyak atau sedikit untuk wilayah tersebut.
3. Tulis paragraf komposisi (2-3 kalimat): bahas komposisi jenjang dan status yang dominan. Sertakan angka dan persentase persis dari data.
4. Jika {ditampilkan} < {total}: tulis kalimat yang menginformasikan ada {total - ditampilkan:,} sekolah lagi yang tidak ditampilkan. Sarankan user memfilter lebih spesifik: berdasarkan jenjang, status, akreditasi, atau nama kecamatan.
5. [HANYA jika ini query QC/residu]: Bahas masalah data secara mendalam. Untuk setiap sekolah yang disebutkan, jelaskan masalahnya secara spesifik — jangan hanya menyebut nama sekolah tanpa konteks masalahnya.{_qc_note_instruksi}
6. Tutup dengan 1 kalimat yang mengundang pertanyaan lanjutan, misalnya: "Apabila Anda membutuhkan informasi lebih detail mengenai sekolah tertentu, silakan tanyakan."
7. JANGAN tulis analisis kebijakan, opini, atau komentar di luar data yang disuplai.
8. JANGAN ulangi data yang sudah ada di tabel dalam bentuk bullet point.{_ambiguity_disclaimer}

NARASI:"""


def _prompt_school_profile(profile_data: Dict[str, Any], query: str = "") -> str:
    f          = _extract_profile_fields(profile_data)
    nama       = f["nama"]
    bentuk     = f["bentuk"]
    status     = f["status"]
    akreditasi = f["akreditasi"]
    nilai_akhir = f["nilai_akhir"]
    lokasi_str = ", ".join(filter(None, [f["kec"], f["kab"], f["prov"]])) or "-"
    kode_pos   = f["kode_pos"]

    guru_total = f["guru_total"]
    guru_l     = f["guru_l"]
    guru_p     = f["guru_p"]
    guru_pns   = f["guru_pns"]
    guru_honor = f["guru_honor"]
    guru_serti = f["guru_serti"]
    guru_s1    = f.get("guru_s1_keatas", 0)

    tendik_total = f["tendik_total"]
    tendik_l     = f["tendik_l"]
    tendik_p     = f["tendik_p"]

    siswa_total = f["siswa"]
    siswa_l     = f["siswa_l"]
    siswa_p     = f["siswa_p"]
    internet    = f["internet"]
    listrik     = f["listrik"]
    air         = f["air"]

    # PATCH BARU
    jamban      = f.get("tipe_jamban", "-")
    kec_air     = f.get("kecukupan_air", "-")
    minum       = f.get("minum_siswa", 0)
    toilet_l    = f.get("toilet_l", 0)
    toilet_p    = f.get("toilet_p", 0)
    komputer    = f.get("total_komputer", 0)
    tahun_data  = f.get("tahun_data", "-")


    # Komposisi siswa per gender
    if siswa_total and siswa_l + siswa_p > 0:
        pct_p = round(siswa_p / siswa_total * 100) if siswa_total else 0
        siswa_gender_str = f"L: {siswa_l}, P: {siswa_p} ({pct_p}% perempuan)"
    else:
        siswa_gender_str = "data gender tidak tersedia"

    # Guru string dengan rasio dan persentase
    if guru_total > 0:
        pct_serti = round(guru_serti / guru_total * 100) if guru_serti else 0
        pct_s1    = round(guru_s1    / guru_total * 100) if guru_s1    else 0
        rasio_guru = f", rasio 1:{round(siswa_total / guru_total)}" if siswa_total else ""
        guru_str  = (
            f"{guru_total} (L: {guru_l}, P: {guru_p}) — "
            f"PNS: {guru_pns}, Honorer: {guru_honor}, "
            f"Sertifikasi: {guru_serti} ({pct_serti}%), "
            f"S1+: {guru_s1} ({pct_s1}%){rasio_guru}"
        )
    else:
        guru_str  = "belum tercatat"
        pct_serti = 0
        pct_s1    = 0

    siswa_str = f"{siswa_total} ({siswa_gender_str})" if siswa_total else "belum tercatat"

    _kapasitas_narasi = (
        f"PTK {guru_total} guru — Sertifikasi {pct_serti}%, S1+ {pct_s1}%, "
        f"PNS {guru_pns} vs Honorer {guru_honor}. "
        f"Interpretasikan kualitas SDM dan rasio guru:siswa."
        if guru_total > 0 else
        "PTK belum tercatat — sampaikan tanpa menyimpulkan tidak ada guru."
    )

    # Catatan kritis
    catatan_kritis = []

    if internet in ("Tidak Ada", "-", "", None):
        catatan_kritis.append("tidak ada akses internet")

    if listrik in ("Tidak Ada", "-", "", None):
        catatan_kritis.append("sumber listrik tidak tercatat")

    if air in ("Air hujan", ""):
        catatan_kritis.append(f"sumber air: {air} (perlu perhatian)")

    if f.get("tipe_jamban") in (None, "-", "Tidak Ada"):
        catatan_kritis.append("fasilitas jamban tidak tercatat atau tidak ada")

    if f.get("toilet_l", 0) == 0 and f.get("toilet_p", 0) == 0:
        catatan_kritis.append("jumlah toilet siswa: tidak ada data (0)")

    catatan_str = "; ".join(catatan_kritis) if catatan_kritis else "tidak ada catatan kritis"

    akred_konteks = {
        "A": "akreditasi terbaik — standar mutu tinggi",
        "B": "akreditasi baik — masih ada ruang peningkatan menuju A",
        "C": "akreditasi cukup — perlu peningkatan di beberapa aspek",
        "-": "belum terakreditasi atau data tidak tersedia",
    }.get(akreditasi, "")

    nilai_akhir_str = (
        f"\n- Nilai Akhir Akreditasi: {nilai_akhir}"
        if nilai_akhir is not None else ""
    )

    tendik_str = (
        f"{tendik_total} (L: {tendik_l}, P: {tendik_p})"
        if tendik_total > 0 else "tidak tercatat"
    )

    kode_pos_str = kode_pos if kode_pos not in (None, "-", "") else "tidak tersedia"

    # ── QC / Kualitas Data ─────────────────────────────────────────────
    _QC_NAMES = {
        "qc_6": "koordinat lintang", "qc_7": "koordinat bujur",
        "qc_12": "foto sekolah", "qc_13": "data yayasan",
        "qc_8": "SK izin operasional", "qc_10": "SK pendirian",
        "qc_4": "alamat jalan", "qc_3": "nama sekolah",
    }
    qc = profile_data.get("kualitas_data", {}) or {}
    total_masalah_qc = qc.get("total_masalah")
    persen_bersih_qc = qc.get("persen_bersih")
    qc_labels = qc.get("qc_labels", {}) or {}
    _is_negeri = (f.get("status") or "").upper() == "NEGERI"
    masalah_list = [
        _QC_NAMES.get(k, k)
        for k, v in qc_labels.items()
        if v and v != "OK"
        and not (k == "qc_13" and _is_negeri)
    ]
    _qc_13_val = qc_labels.get("qc_13", "OK")
    _qc_13_counted = _is_negeri and _qc_13_val and _qc_13_val != "OK"
    if _qc_13_counted and total_masalah_qc:
        total_masalah_qc = max(0, total_masalah_qc - 1)
        persen_bersih_qc = round((13 - total_masalah_qc) / 13 * 100, 2)
    if total_masalah_qc is not None and total_masalah_qc > 0:
        qc_str = (
            f"{total_masalah_qc} masalah ditemukan ({persen_bersih_qc}% bersih). "
            f"Kolom bermasalah: {', '.join(masalah_list)}."
        )
        masalah_joined = ", ".join(masalah_list) if masalah_list else "lihat tabel kualitas"

        # Bangun konteks per-field dari QC_CONTEXT_LIBRARY
        _qc_context_lines = []
        for qc_key, qc_val in qc_labels.items():
            if qc_val and qc_val != "OK" and not (qc_key == "qc_13" and _is_negeri):
                ctx = QC_CONTEXT_LIBRARY.get(qc_key)
                if ctx:
                    _qc_context_lines.append(
                        f"  • {ctx['nama'].upper()}: "
                        f"Penyebab — {ctx['penyebab_umum']} "
                        f"Dampak — {ctx['dampak']} "
                        f"Rekomendasi — {ctx['rekomendasi']}"
                    )
        _qc_context_block = "\n".join(_qc_context_lines) if _qc_context_lines else ""

        qc_narasi_instruksi = f"""

## Kualitas & Kelengkapan Data (WAJIB — ada {total_masalah_qc} masalah)
Tulis paragraf ini. Sebutkan EKSPLISIT kolom yang bermasalah: {masalah_joined}.
Gunakan kalimat: "Data yang perlu dilengkapi meliputi: {masalah_joined}."
Jelaskan MENGAPA setiap field bermasalah (gunakan konteks di bawah), BUKAN hanya menyebut bahwa bermasalah.
{_qc_context_block}
"""
        n_paragraf = "5"
    elif total_masalah_qc == 0:
        qc_str = "Data lengkap (100% bersih)."
        qc_narasi_instruksi = ""
        n_paragraf = "4"
    else:
        qc_str = "Data kualitas tidak tersedia."
        qc_narasi_instruksi = ""
        n_paragraf = "4"

    _q = query.strip() if query else ""
    _konteks_query = (
        f'KONTEKS PERTANYAAN USER: "{_q}"\n\n'
        f'PEMBUKA WAJIB (1 kalimat sebelum ## Identitas & Posisi):\n'
        f'Awali narasi dengan satu kalimat yang merangkum apa yang ditanyakan user.\n'
        f'Contoh: "Berikut adalah informasi mengenai masalah data pada SMAN 28 Kabupaten Tangerang."\n'
        f'atau: "Berikut profil lengkap TK FAZZA di Kota Bandung."\n'
        f'JANGAN mulai langsung dengan nama sekolah tanpa konteks.\n\n'
        if _q else ""
    )

    return f"""{_SYSTEM_BASE}

DATA SEKOLAH:
- Nama       : {nama}
- Jenjang    : {bentuk} | Status: {status} | Akreditasi: {akreditasi} ({akred_konteks}){nilai_akhir_str}
- Lokasi     : {lokasi_str} | Kode Pos: {kode_pos_str}
- Guru (PTK) : {guru_str}
- Tendik     : {tendik_str}
- Siswa      : {siswa_str}
- Internet   : {internet} | Listrik: {listrik} | Air: {air}
- Sanitasi   : Tipe Jamban: {jamban} | Kecukupan Air: {kec_air} | Air Minum Siswa: {minum} unit
- Toilet     : Laki-laki {toilet_l} | Perempuan {toilet_p}
- Komputer   : {komputer} unit
- Tahun Data : {tahun_data}
- Catatan    : {catatan_str}
- Kualitas Data: {qc_str}

{_konteks_query}INSTRUKSI NARASI ({n_paragraf} paragraf dengan ## heading, TANPA bullet):

## Identitas & Posisi
Sebutkan nama sekolah PERSIS "{nama}" (jangan diubah sama sekali).
Sebutkan jenjang {bentuk}, status {status}, lokasi {lokasi_str}.
Interpretasikan akreditasi {akreditasi}: {akred_konteks}.
Jika nilai_akhir tersedia: sebutkan sebagai skor akreditasi numerik.

## Kapasitas Siswa & Pengajar
Sebutkan total siswa beserta breakdown gender ({siswa_gender_str}).
{_kapasitas_narasi}
Jika tendik>0: sebutkan jumlah tenaga kependidikan.

## Komposisi & Stabilitas SDM
(Hanya jika PTK>0) Interpretasikan proporsi PNS vs honorer.
Jika honorer > PNS: sorot potensi dampak stabilitas jangka panjang.
Jika PNS dominan: sorot stabilitas yang baik.

## Sarana & Rekomendasi
Komentari kondisi internet, listrik, air, sanitasi, toilet, komputer secara jujur.
Jika ada kondisi kritis (dari catatan): SOROT sebagai temuan penting.
Tutup dengan satu kalimat insight keseluruhan.{qc_narasi_instruksi}
PENTING: Gunakan nama "{nama}" PERSIS. JANGAN tulis "Sekolah dengan NPSN X" atau "Sekolah ini".
PENTING: Jangan ulang angka yang sudah ada di tabel — fokus pada INTERPRETASI.

INSTRUKSI FORMAT OUTPUT:
- Tulis narasi dalam SATU output terpadu, bukan dua bagian.
- Awali dengan paragraf pembuka berisi identitas sekolah (nama, jenjang, status, lokasi lengkap).
- Lanjutkan dengan paragraf tentang SDM (guru, tendik, siswa) — sertakan angka persis.
- Lanjutkan dengan paragraf tentang fasilitas (sumber air, listrik, internet, jamban, toilet, komputer).
- Jika ada masalah QC, jelaskan di paragraf tersendiri: penyebab, dampak, dan rekomendasi.
- Akhiri dengan kalimat penutup yang mengundang pertanyaan lanjutan.
- Jangan tulis heading '## Identitas', '## Lokasi', atau bullet list — tulis paragraf mengalir.

NARASI:"""


# ─────────────────────────────────────────────────────────────────────────────
# MULTI-METRIC AGGREGATION PROMPT (Phase 21-A)
# ─────────────────────────────────────────────────────────────────────────────

def build_multi_metric_aggregation_prompt(
    wilayah: str,
    level: str,
    multi_metric: Dict[str, Any],
    active_filters: Optional[Dict] = None,
) -> str:
    """Prompt narasi untuk aggregation multi-metric (dua atau lebih metric sekaligus)."""
    level_label = {
        "desa_kelurahan": "desa/kelurahan",
        "kecamatan":      "kecamatan",
        "kabupaten":      "kabupaten/kota",
        "provinsi":       "provinsi",
        "nasional":       "nasional",
        "pulau":          "pulau",
    }.get(level, level)

    data_lines = []
    for metric_key, data in multi_metric.items():
        label = data["label"]
        total = data["total"]
        data_lines.append(f"- {label.title()}: {total:,}".replace(",", "."))
        if data.get("drilldown_top3"):
            dd_field = data.get("drilldown_field", "")
            dd_label = {"kabupaten": "Kab/Kota", "kecamatan": "Kecamatan", "provinsi": "Provinsi"}.get(dd_field, dd_field.title())
            for i, item in enumerate(data["drilldown_top3"][:3], 1):
                data_lines.append(
                    f"  {i}. {item.get('name','').title()}: {item.get('count', 0):,}".replace(",", ".")
                    + f" {label}"
                )

    data_str = "\n".join(data_lines)

    filter_hints = []
    if active_filters:
        if active_filters.get("bentuk_pendidikan"):
            filter_hints.append(f"jenjang {active_filters['bentuk_pendidikan']}")
        if active_filters.get("status_sekolah"):
            filter_hints.append(f"status {active_filters['status_sekolah']}")
    filter_note = f" (filter: {', '.join(filter_hints)})" if filter_hints else ""

    return (
        f"{_SYSTEM_BASE}\n\n"
        f"DATA AGREGASI MULTI-METRIK di {wilayah} (level: {level_label}){filter_note}:\n"
        f"{data_str}\n\n"
        f"INSTRUKSI NARASI — Format dengan heading markdown ## per metric:\n"
        f"1. Buat heading ## untuk setiap metric (contoh: ## Siswa Laki-laki).\n"
        f"2. Di bawah setiap heading: satu kalimat angka total + kalimat distribusi top 3 wilayah (jika ada).\n"
        f"3. Di akhir: satu paragraf '## Catatan' yang merangkum perbandingan antar metric secara singkat.\n"
        f"4. Angka PERSIS seperti data — jangan bulatkan atau ubah. Format: titik sebagai pemisah ribuan.\n"
        f"5. Satuan: guru/siswa/tendik = 'orang', sekolah = 'sekolah', komputer = 'unit'.\n"
        f"LARANGAN: Jangan sebut 'satuan pendidikan' untuk metric guru/siswa — gunakan 'orang'.\n"
        f"LARANGAN: Jangan menambahkan angka atau fakta di luar data yang diberikan.\n\n"
        f"OUTPUT:"
    )

def _prompt_ooc(query: str) -> str:
    return _prompt_ooc_guardrail(query)
