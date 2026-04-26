# services/narrator_service.py
"""
NarratorService — mengubah data terstruktur dari ES menjadi narasi natural
menggunakan DeepSeek. Dipanggil setelah data valid didapat, sebelum response
dikembalikan ke user.

Prinsip:
- AI TIDAK mencari data → tidak bisa hallucinate angka
- Semua angka/lokasi disuntikkan eksplisit ke prompt
- Jika AI gagal → fallback ke template (tidak pernah error ke user)
- Timeout pendek (8 detik) agar latency terjaga
"""

import asyncio
import logging
import os
from typing import Dict, Any, List, Optional

from app.providers.deepseek import call_deepseek, DeepSeekUnavailableError

logger = logging.getLogger(__name__)

# Timeout khusus narasi — lebih pendek dari total request budget (~30s)
# Dengan retry_strategy di deepseek.py (2 attempts, 1s wait):
#   worst-case = TIMEOUT * 2 + 1 ≈ 25s (simple) / 31s (aggregation with breakdown)
# asyncio.wait_for di _call_narrator memastikan hard-cap tidak terlampaui.
# Grace period +5s antara call_deepseek(timeout=T) dan wait_for(timeout=T+5)
# mencegah race condition: asyncio membunuh coroutine SEBELUM tenacity sempat retry.
# ── TIMEOUT CONFIG ──
NARRATOR_TIMEOUT             = 20   # simple narasi — hard cap 20s (naik dari 10s)
NARRATOR_TIMEOUT_AGGREGATION = 25   # aggregation + breakdown — lebih longgar (naik dari 13s)
_NARRATOR_WAIT_FOR_GRACE     = 5    # detik ekstra untuk asyncio.wait_for di atas timeout LLM

# ── TOKEN CONFIG ──
NARRATOR_TOKENS_PROFILE      = 900
NARRATOR_TOKENS_AGGREGATION  = 750
NARRATOR_TOKENS_COMPARISON   = 700
NARRATOR_TOKENS_LIST         = 500
NARRATOR_TOKENS_OOC          = 650

# ── TEMP CONFIG ──
NARRATOR_TEMP                = 0.4


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
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
    "- Panjang respons: 150-300 kata untuk query sederhana, "
    "  300-500 kata untuk query multi-modul atau kompleks.\n\n"

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
# PROMPT BUILDERS  (satu fungsi per query-type)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# VALIDATOR — Cek angka kritis tidak berubah setelah narasi LLM
# ─────────────────────────────────────────────────────────────────────────────

def _validate_numbers_in_narasi(narasi: str, expected_numbers) -> Optional[str]:
    """
    Return narasi jika semua angka kritis masih ada di teks.
    Return None jika ada angka yang hilang — caller harus fallback ke template.
    Menerima format: 1234, 1.234, 1,234, 1 234 (pemisah ribuan berbeda-beda).
    Menerima expected_numbers sebagai dict {label: value} atau list [value, ...].
    """

    import re as _re

    def _normalize(text: str) -> str:
        # Hapus semua pemisah ribuan (titik, koma, spasi)
        return _re.sub(r"[.,\s]", "", text)

    # Normalisasi narasi
    narasi_normalized = _normalize(narasi)

    # Normalise ke list of (label, val) pairs agar bisa terima dict atau list
    if isinstance(expected_numbers, dict):
        items = list(expected_numbers.items())
    else:
        items = [(str(v), v) for v in expected_numbers]

    for label, val in items:
        if val is None:
            continue

        val_str = _normalize(str(int(val)))

        if val_str not in narasi_normalized:
            logger.warning(
                "[Narrator] Angka kritis hilang dari narasi — label=%s expected=%s",
                label, val
            )
            return None

    return narasi


# ─────────────────────────────────────────────────────────────────────────────
# HALLUCINATION & NUMBER EXTRACTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_HALLUCINATION_PATTERNS = [
    r'\bberdasarkan (pengetahuan|pelatihan|data) saya\b',
    r'\bmenurut (pengetahuan|pelatihan) saya\b',
    r'\bsaya tidak (memiliki|punya) (akses|data)\b',
    r'\bsebagai (ai|llm|model bahasa)\b',
    r'\b(gpt|chatgpt|openai|gemini|llama)\b',
]


def _contains_hallucination(text: str) -> bool:
    """Return True if text contains known LLM hallucination patterns (case-insensitive)."""
    import re as _re
    text_lower = text.lower()
    return any(_re.search(p, text_lower) for p in _HALLUCINATION_PATTERNS)


def _extract_numbers_from_text(text: str) -> list:
    """Ekstrak semua angka signifikan (>= 10) dari teks. Handle format Indonesia (1.234)."""
    import re as _re
    pattern = r'\b\d{1,3}(?:[.,]\d{3})*|\b\d{2,}\b'
    raw = _re.findall(pattern, text)
    result = []
    for r in raw:
        normalized = r.replace('.', '').replace(',', '')
        try:
            val = int(normalized)
            if val >= 10:
                result.append(val)
        except ValueError:
            pass
    return list(set(result))


def _build_qc_summary_context(schools: list) -> str:
    """
    Bangun ringkasan konteks QC dari list sekolah untuk dikirim ke LLM narrator.

    Digunakan ketika query adalah query residu (qc_masalah_only=True).
    Menghasilkan teks terstruktur yang membantu LLM membuat narasi yang akurat
    tentang masalah data sekolah tanpa halusinasi.

    Args:
        schools: list dict hasil dari _format_hit(), sudah mengandung
                 total_masalah, persen_bersih, masalah_detail

    Returns:
        String ringkasan untuk dimasukkan ke context LLM.
    """
    if not schools:
        return "Tidak ada sekolah ditemukan."

    has_qc_data = any(s.get("total_masalah") is not None for s in schools)
    if not has_qc_data:
        return ""  # bukan query residu, kembalikan kosong

    lines = []
    for s in schools:
        nama = s.get("nama", "Tidak diketahui")
        npsn = s.get("npsn", "-")
        total = s.get("total_masalah")
        persen = s.get("persen_bersih")
        detail = s.get("masalah_detail", "")

        # Ekstrak daftar masalah dari content
        masalah_list = []
        if detail:
            import re
            m = re.search(r"Masalah data:\s*(.+?)\.?\s*$", detail, re.IGNORECASE | re.DOTALL)
            if m:
                masalah_list = [x.strip() for x in m.group(1).split(",") if x.strip()]

        line = f"- {nama} (NPSN: {npsn})"
        if total is not None:
            line += f": {total} masalah"
        if persen is not None:
            line += f", {persen:.1f}% bersih"
        if masalah_list:
            line += f". Masalah: {', '.join(masalah_list)}"
        lines.append(line)

    header = f"Ditemukan {len(schools)} sekolah dengan masalah data:\n"
    return header + "\n".join(lines)


def _zero_result_message(
    wilayah: str,
    active_filters: dict = None,
    modul: str = "aggregation",
) -> str:
    """
    Pesan informatif ketika ES mengembalikan 0 hasil.
    Lebih berguna dari template kosong atau narasi canggung.
    """
    filter_hints = []
    if active_filters:
        if active_filters.get("bentuk_pendidikan") or active_filters.get("bentuk"):
            jenjang = active_filters.get("bentuk_pendidikan") or active_filters.get("bentuk")
            filter_hints.append(f"jenjang **{jenjang}**")
        if active_filters.get("status_sekolah") or active_filters.get("status"):
            status = active_filters.get("status_sekolah") or active_filters.get("status")
            filter_hints.append(f"status **{status}**")
        if active_filters.get("akreditasi_exact") or active_filters.get("akreditasi"):
            akred = active_filters.get("akreditasi_exact") or active_filters.get("akreditasi")
            filter_hints.append(f"akreditasi **{akred}**")

    wilayah_str = f"**{wilayah}**" if wilayah else "wilayah yang dimaksud"
    filter_str  = " dengan filter " + ", ".join(filter_hints) if filter_hints else ""

    saran_parts = []
    if filter_hints:
        saran_parts.append("coba hapus salah satu filter")
    saran_parts.append("perluas cakupan wilayah ke level kecamatan atau kabupaten")

    saran_str = " atau ".join(saran_parts)

    return (
        f"Tidak ditemukan satuan pendidikan di {wilayah_str}{filter_str} "
        f"yang sesuai dengan kriteria pencarian.\n\n"
        f"**Saran:** {saran_str.capitalize()}."
    )


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
) -> str:
    status_info   = f" ({status_filter})" if status_filter else ""
    konteks_bench = _benchmark_label(total, level)
    level_label   = {
        "desa_kelurahan": "desa/kelurahan",
        "kecamatan":      "kecamatan",
        "kabupaten":      "kabupaten/kota",
        "provinsi":       "provinsi",
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

        status_lines = ", ".join(
            f"{k}: {v:,}"
            for k, v in sorted(status_clean.items(), key=lambda x: -x[1])
            if k in ("NEGERI", "SWASTA")  # hanya tampilkan dua utama
        )
        total_ns  = status_clean.get("NEGERI", 0) + status_clean.get("SWASTA", 0)
        swasta_ct = status_clean.get("SWASTA", 0)
        pct_sw    = round(swasta_ct / total_ns * 100) if total_ns else 0
        breakdown_section += (
            f"\n- Breakdown status: {status_lines} ({pct_sw}% swasta)"
        )

    # ── Drilldown top 3 ──────────────────────────────────────────────────────
    drilldown_section = ""
    if drilldown_top3 and drilldown_field:
        drilldown_label = {
            "kabupaten": "Kabupaten/Kota",
            "kecamatan": "Kecamatan",
            "desa_kelurahan": "Desa/Kelurahan",
        }.get(drilldown_field, drilldown_field.title())

        drilldown_lines = "\n".join(
            f"  {i+1}. {item['name'].title()}: {item['count']:,} {metric_label}"
            for i, item in enumerate(drilldown_top3[:3])
        )
        drilldown_section = (
            f"\n- Top 3 {drilldown_label} terbanyak:\n{drilldown_lines}"
        )

    has_breakdown = bool(breakdown_section or drilldown_section)

    # ── Instruksi narasi ─────────────────────────────────────────────────────
    if has_breakdown:
        drilldown_instruksi = ""
        if drilldown_section:
            drilldown_instruksi = (
                "\n\n## Distribusi Wilayah\n"
                f"Sebutkan top 3 {drilldown_label} dengan jumlah {metric_label} "
                "terbanyak beserta angkanya — gunakan PERSIS data yang disuplai."
            )

        pct_instruksi = ""
        if total_all and pct_of_total is not None:
            pct_instruksi = (
                f"\n\n## Persentase\n"
                f"Sebutkan bahwa {total:,} sekolah ({pct_of_total}%) dari total "
                f"{total_all:,} sekolah di wilayah ini memenuhi kriteria filter."
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
            "Interpretasikan proporsi negeri vs swasta — angka dan persentase "
            "harus persis dari data."
            + pct_instruksi
            + drilldown_instruksi +
            "\n\n## Implikasi\n"
            "Satu kalimat nilai praktis untuk perencanaan pendidikan."
        )
    else:
        instruksi = (
            "INSTRUKSI NARASI (3 kalimat tepat, TANPA bullet point, TANPA heading):\n"
            f"Kalimat 1: Sebutkan PERSIS angka {total:,} {metric_label} di {wilayah} "
            f"({level_label}). Jangan bulatkan atau ubah angka ini.\n"
            "Kalimat 2: Bandingkan dengan benchmark — gunakan label konteks yang "
            "sudah disediakan (banyak/sedang/sedikit).\n"
            "Kalimat 3: Satu implikasi praktis bagi perencanaan pendidikan di wilayah ini.\n"
            f"LARANGAN KERAS: Jangan tulis angka selain {total:,} untuk jumlah {metric_label}."
        )

    # ── Filter aktif & persentase ────────────────────────────────────────────
    filter_section = ""
    if active_filters:
        parts = []
        if active_filters.get("bentuk_pendidikan"):
            parts.append(f"jenjang {active_filters['bentuk_pendidikan']}")
        if active_filters.get("status_sekolah"):
            parts.append(f"status {active_filters['status_sekolah']}")
        if active_filters.get("akreditasi_gte"):
            grades = "/".join(active_filters["akreditasi_gte"])
            parts.append(f"akreditasi {grades}")
        elif active_filters.get("akreditasi_exact"):
            parts.append(f"akreditasi {active_filters['akreditasi_exact']}")
        if parts:
            filter_section = f"\n- Filter aktif: {', '.join(parts)}"

    pct_section = ""
    if total_all and pct_of_total is not None:
        pct_section = (
            f"\n- Total SEMUA sekolah di wilayah (tanpa filter): {total_all:,}"
            f"\n- Persentase yang memenuhi filter: {pct_of_total}%"
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
- Konteks      : jumlah ini {konteks_bench}{breakdown_section}{drilldown_section}

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
        "siswa":            "siswa",
        "siswa_l":          "siswa laki-laki",
        "siswa_p":          "siswa perempuan",
        "guru":             "guru/PTK",
        "guru_pns":         "guru PNS",
        "guru_honor":       "guru honorer",
        "guru_sertifikasi": "guru bersertifikasi",
        "tendik":           "tenaga kependidikan",
    }

    metric_label = _METRIC_LABELS.get(metric_target, "satuan pendidikan") if metric_target else "satuan pendidikan"
    is_numeric_metric = metric_target and metric_target != "sekolah"
    school_lines = "\n".join(
        f"- {s.get('nama', 'Nama tidak tersedia')} "
        f"({s.get('kecamatan', '-')})"
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
    if _wants_avg and avg_section:
        instruksi = (
            "INSTRUKSI NARASI (4-5 kalimat, TANPA bullet point, TANPA heading):\n"
            "1. JAWAB langsung: sebutkan rata-rata siswa/sekolah masing-masing wilayah dengan angka persis.\n"
            "2. Nyatakan wilayah mana yang lebih padat berdasarkan rata-rata — bukan jumlah sekolah.\n"
            f"3. Jika ada filter jenjang ({_jenjang_req or 'semua'}), sebutkan secara eksplisit.\n"
            "4. Boleh sebut jumlah sekolah sebagai konteks pendukung, bukan jawaban utama.\n"
            "5. Tutup dengan satu kalimat interpretasi singkat."
        )
    elif _jenjang_req:
        instruksi = (
            f"INSTRUKSI NARASI (4-5 kalimat, TANPA bullet point, TANPA heading):\n"
            f"1. Fokus pada jenjang {_jenjang_req}: sebutkan jumlahnya di masing-masing wilayah.\n"
            "2. JANGAN jadikan total sekolah semua jenjang sebagai jawaban utama.\n"
            "3. Sebutkan selisih dan interpretasikan signifikansinya.\n"
            "4. Jika ada breakdown status: sebutkan proporsi negeri vs swasta.\n"
            "5. Tutup dengan catatan praktis."
        )
    else:
        instruksi = (
            "INSTRUKSI NARASI (4-5 kalimat, TANPA bullet point, TANPA heading):\n"
            "1. Sebutkan kedua wilayah dan jumlah sekolah masing-masing secara eksplisit.\n"
            "2. Sebutkan selisih absolut dan persentase — interpretasikan signifikansinya.\n"
            "3. Jika ada breakdown jenjang: sebutkan jenjang dominan di masing-masing wilayah.\n"
            "4. Jika ada breakdown status: interpretasikan proporsi negeri vs swasta.\n"
            "5. Tutup dengan catatan praktis: apa yang perlu didalami lebih lanjut."
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
            f"Nama: {s.get('nama','-')} | Jenjang: {s.get('bentuk_pendidikan','-')} | "
            f"Status: {s.get('status_sekolah','-')} | Akreditasi: {s.get('akreditasi','-')} | "
            f"Kecamatan: {s.get('kecamatan','-')} | Siswa: data tidak tersedia | "
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

INSTRUKSI NARASI (3 kalimat + 1 tabel ringkas, TANPA bullet point di narasi):
- Tampilkan tabel markdown 2 kolom (Status | Jumlah) SEBELUM narasi.
- Kalimat 1: Gambaran total dan komposisi negeri vs swasta secara eksplisit.
- Kalimat 2: Interpretasi dominasi — sebutkan mana yang lebih banyak dan \
seberapa jauh selisihnya.
- Kalimat 3: Konteks singkat mengapa pola ini umum terjadi \
(mis. kota besar cenderung banyak swasta).

NARASI:"""


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
        nama     = s.get('nama', '-')
        jenjang  = s.get('bentuk_pendidikan', s.get('bentuk', '-'))
        akred    = s.get('akreditasi', '-')
        siswa    = '-'
        kec      = s.get('kecamatan', '-')
        rows.append(f"| {i} | {nama} | {jenjang} | {akred} | {siswa} | {kec} |")

    return header + sep + "\n".join(rows)

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
) -> str:
    _LEVEL_LABEL = {
        "provinsi": "Provinsi",
        "kabupaten": "Kabupaten/Kota",
        "kecamatan": "Kecamatan",
        "desa_kelurahan": "Desa/Kelurahan",
    }
    level_desc = _LEVEL_LABEL.get(wilayah_level, "")
    wilayah_full = f"{level_desc} {wilayah}".strip() if level_desc else wilayah
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
            "- Jangan sebutkan breakdown akreditasi kecuali diminta\n"
            f"\nData masalah:\n{qc_summary}"
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
{school_table}

INSTRUKSI NARASI:
1. Tampilkan tabel di atas PERSIS seperti adanya di bagian pertama respons.
2. Tulis 3-4 kalimat ringkasan: sebutkan total {total:,} sekolah, filter aktif, dan komposisi dominan.
3. Jika {ditampilkan} < {total:,}: informasikan bahwa masih ada data lain dan sarankan filter lebih spesifik.
4. JANGAN tulis narasi panjang tentang distribusi wilayah atau implikasi — user minta LIST, bukan analisis.

NARASI:"""


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
    # Field dari profile_builder: tenaga_pendidik.total_guru (bukan total_ptk)
    guru_total = _int(["tenaga_pendidik.total_guru", "jumlah_ptk", "total_ptk", "total_guru"])
    guru_l     = _int(["tenaga_pendidik.laki_laki",  "guru_l"])
    guru_p     = _int(["tenaga_pendidik.perempuan",  "guru_p"])
    guru_pns   = _int(["tenaga_pendidik.guru_pns",   "guru_pns"])
    guru_honor = _int(["tenaga_pendidik.guru_honor", "guru_honor"])
    guru_serti = _int(["tenaga_pendidik.guru_sertifikasi", "guru_sertifikasi"])

    if guru_total == 0 and (guru_pns + guru_honor) > 0:
        guru_total = guru_pns + guru_honor

    # ── Tendik ────────────────────────────────────────────────────────────────
    tendik_total = _int(["tenaga_kependidikan.total_tendik", "tendik_total"])
    tendik_l     = _int(["tenaga_kependidikan.laki_laki",    "tendik_l"])
    tendik_p     = _int(["tenaga_kependidikan.perempuan",    "tendik_p"])
    if tendik_total == 0 and (tendik_l + tendik_p) > 0:
        tendik_total = tendik_l + tendik_p

    # ── Siswa ─────────────────────────────────────────────────────────────────
    siswa_total = _int(["peserta_didik.total_siswa", "total_siswa", "jumlah_siswa"])
    siswa_l     = _int(["peserta_didik.laki_laki",   "siswa_l"])
    siswa_p     = _int(["peserta_didik.perempuan",   "siswa_p"])

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


def _prompt_school_profile(profile_data: Dict[str, Any]) -> str:
    f          = _extract_profile_fields(profile_data)
    nama       = f["nama"]
    bentuk     = f["bentuk"]
    status     = f["status"]
    akreditasi = f["akreditasi"]
    nilai_akhir = f["nilai_akhir"]
    lokasi_str = ", ".join(filter(None, [f["kec"], f["kab"], f["prov"]])) or "-"
    kode_pos   = f["kode_pos"]

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
    masalah_list = [
        _QC_NAMES.get(k, k)
        for k, v in qc_labels.items()
        if v and v != "OK"
    ]
    if total_masalah_qc is not None and total_masalah_qc > 0:
        qc_str = (
            f"{total_masalah_qc} masalah ditemukan ({persen_bersih_qc}% bersih). "
            f"Kolom bermasalah: {', '.join(masalah_list)}."
        )
        masalah_joined = ", ".join(masalah_list) if masalah_list else "lihat tabel kualitas"
        qc_narasi_instruksi = f"""

## Kualitas & Kelengkapan Data (WAJIB — ada {total_masalah_qc} masalah)
Tulis paragraf ini. Sebutkan EKSPLISIT kolom yang bermasalah: {masalah_joined}.
Gunakan kalimat: "Data yang perlu dilengkapi meliputi: {masalah_joined}."
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

    return f"""{_SYSTEM_BASE}

DATA SEKOLAH:
- Nama       : {nama}
- Jenjang    : {bentuk} | Status: {status} | Akreditasi: {akreditasi} ({akred_konteks}){nilai_akhir_str}
- Lokasi     : {lokasi_str} | Kode Pos: {kode_pos_str}
- Guru (PTK) : data tidak tersedia di indeks ini
- Tendik     : {tendik_str}
- Siswa      : data tidak tersedia di indeks ini
- Internet   : {internet} | Listrik: {listrik} | Air: {air}
- Sanitasi   : Tipe Jamban: {jamban} | Kecukupan Air: {kec_air} | Air Minum Siswa: {minum} unit
- Toilet     : Laki-laki {toilet_l} | Perempuan {toilet_p}
- Komputer   : {komputer} unit
- Tahun Data : {tahun_data}
- Catatan    : {catatan_str}
- Kualitas Data: {qc_str}

INSTRUKSI NARASI ({n_paragraf} paragraf dengan ## heading, TANPA bullet):

## Identitas & Posisi
Sebutkan nama sekolah PERSIS "{nama}" (jangan diubah sama sekali).
Sebutkan jenjang {bentuk}, status {status}, lokasi {lokasi_str}.
Interpretasikan akreditasi {akreditasi}: {akred_konteks}.
Jika nilai_akhir tersedia: sebutkan sebagai skor akreditasi numerik.

## Kapasitas Siswa & Pengajar
Sebutkan total siswa beserta breakdown gender ({siswa_gender_str}).
Lalu bahas PTK dan tendik:
- Jika PTK=0: sampaikan data belum tercatat, JANGAN simpulkan tidak ada guru.
- Jika PTK>0: interpretasikan rasio guru:siswa, persentase sertifikasi.
- Jika tendik>0: sebutkan jumlah tenaga kependidikan.

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

NARASI:"""


def _prompt_ooc(query: str) -> str:
    return _prompt_ooc_guardrail(query)
# ─────────────────────────────────────────────────────────────────────────────
# CORE CALLER  (tidak berubah dari versi sebelumnya)
# ─────────────────────────────────────────────────────────────────────────────

async def _call_narrator(
    prompt: str,
    fallback: str,
    max_tokens: int,
    timeout: int = NARRATOR_TIMEOUT,
) -> str:
    if os.getenv("NARRATOR_ENABLED", "true").lower() != "true":
        logger.debug("[Narrator] Disabled via NARRATOR_ENABLED=false")
        return fallback

    try:
        # asyncio.wait_for memberikan hard-cap waktu total, termasuk retry internal.
        # Grace period +5s memberi ruang tenacity untuk retry tanpa race condition:
        # call_deepseek(timeout=T) menentukan batas per-attempt,
        # wait_for(timeout=T+grace) menentukan batas seluruh coroutine termasuk retry.
        result = await asyncio.wait_for(
            call_deepseek(
                prompt=prompt,
                temperature=NARRATOR_TEMP,
                max_tokens=max_tokens,
                timeout=timeout,
            ),
            timeout=float(timeout) + _NARRATOR_WAIT_FOR_GRACE,
        )
        result = result.strip()

        # strip prefix kalau LLM nambahin label
        for prefix in ("NARASI:", "JAWABAN:", "narasi:", "jawaban:"):
            if result.upper().startswith(prefix.upper()):
                result = result[len(prefix):].strip()
                break

        if _contains_hallucination(result):
            logger.warning(
                "[Narrator] halusinasi terdeteksi, fallback ke template | len=%d",
                len(result),
            )
            return fallback

        return result if result else fallback

    except (asyncio.TimeoutError, asyncio.CancelledError):
        logger.warning("[Narrator] Hard timeout (%ss), using template fallback", timeout)
        return fallback
    except DeepSeekUnavailableError:
        logger.warning("[Narrator] DeepSeek unavailable, using template fallback")
        return fallback
    except Exception:
        logger.exception("[Narrator] Unexpected error, using template fallback")
        return fallback


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — satu fungsi per query-type
# ─────────────────────────────────────────────────────────────────────────────

def detect_level(entities: Dict[str, Any]) -> str:
    """
    Deteksi level administratif terspesifik dari dict entities aggregation.
    Urutan prioritas: desa > kecamatan > kabupaten > provinsi.

    Panggil dari aggregation_handler sebelum narrate_aggregation:
        from services.narrator_service import detect_level
        level = detect_level(entities)
        answer = await narrate_aggregation(wilayah, total, template_md, level=level)
    """
    for lvl in ("desa_kelurahan", "kecamatan", "kabupaten", "provinsi"):
        if entities.get(lvl):
            return lvl
    return "provinsi"

# narrator_service.py
# GANTI narrate_aggregation():

async def narrate_aggregation(
    wilayah: str,
    total: int,
    template_md: str,
    status_filter: Optional[str] = None,
    level: str = "desa_kelurahan",
    breakdown_jenjang: Optional[Dict[str, int]] = None,
    breakdown_status: Optional[Dict[str, int]] = None,
    drilldown_top3: Optional[List[Dict[str, Any]]] = None,
    drilldown_field: Optional[str] = None,
    total_all: Optional[int] = None,
    pct_of_total: Optional[float] = None,
    active_filters: Optional[Dict] = None,
    jenjang_requested: Optional[List[str]] = None,
    metric_target: Optional[str] = None,
) -> str:
    if total == 0:
        return _zero_result_message(wilayah, active_filters, modul="aggregation")
    # Label metric untuk semua metric_target yang didukung (lihat aggregation_core.py)
    _METRIC_LABELS = {
        "siswa":            "siswa",
        "siswa_l":          "siswa laki-laki",
        "siswa_p":          "siswa perempuan",
        "guru":             "guru/PTK",
        "guru_pns":         "guru PNS",
        "guru_honor":       "guru honorer",
        "guru_sertifikasi": "guru bersertifikasi",
        "tendik":           "tenaga kependidikan",
        "sekolah":          "satuan pendidikan",
    }
    metric_label = _METRIC_LABELS.get(metric_target or "sekolah", "satuan pendidikan")

    prompt = _prompt_aggregation(
        wilayah, total, level, status_filter,
        breakdown_jenjang, breakdown_status,
        drilldown_top3, drilldown_field,
        total_all=total_all,
        pct_of_total=pct_of_total,
        active_filters=active_filters,
        jenjang_requested=jenjang_requested,
        metric_target=metric_target,
        metric_label=metric_label,
    )

    has_breakdown = bool(breakdown_jenjang or breakdown_status or drilldown_top3)
    timeout = NARRATOR_TIMEOUT_AGGREGATION if has_breakdown else NARRATOR_TIMEOUT

    narasi = await _call_narrator(
        prompt,
        fallback="",
        max_tokens=NARRATOR_TOKENS_AGGREGATION,
        timeout=timeout,
    )

    if not narasi:
        return template_md

    # Validasi: angka total wajib ada di narasi agar tidak misleading
    expected = {"total": total}
    if total_all:
        expected["total_all"] = total_all

    # Untuk metric numerik (siswa/guru), validasi lebih longgar —
    # angka bisa ditulis dengan format berbeda (4.545.765 vs 4545765)
    skip_validation = bool(metric_target and metric_target not in (None, "sekolah"))

    if not skip_validation and not _validate_numbers_in_narasi(narasi, expected):
        logger.warning("[Narrator] Validasi angka gagal (aggregation), pakai template")
        return template_md

    return f"{template_md}\n\n---\n\n{narasi}"


async def narrate_comparison_lokasi(
    results: List[Dict[str, Any]],
    template_md: str,
    results_detail: Optional[List[Dict[str, Any]]] = None,
    query: str = "",
) -> str:
    if not results or all(r["count"] == 0 for r in results):
        # Bangun pesan kosong yang informatif per wilayah
        wilayah_names = " dan ".join(r.get("wilayah", "?") for r in (results or []))
        return _zero_result_message(wilayah_names, modul="comparison")

    prompt = _prompt_comparison_lokasi(results, results_detail, query=query)
    narasi = await _call_narrator(
        prompt,
        fallback="",
        max_tokens=NARRATOR_TOKENS_COMPARISON,
    )

    if not narasi:
        return template_md

    # Validasi: setiap count wilayah wajib ada di narasi
    expected = {f"count_{i}": r["count"] for i, r in enumerate(results) if r.get("count")}
    if not _validate_numbers_in_narasi(narasi, expected):
        logger.warning("[Narrator] Validasi angka gagal (comparison_lokasi), pakai template")
        return template_md

    return f"{template_md}\n\n---\n\n{narasi}"


async def narrate_comparison_status(
    wilayah: str,
    negeri: int,
    swasta: int,
    pct_diff: int,
    template_md: str,
) -> str:
    """Narasi untuk comparison negeri vs swasta."""
    if negeri == 0 and swasta == 0:
        return _zero_result_message(
            wilayah,
            active_filters={"status_sekolah": "NEGERI/SWASTA"},
            modul="comparison_status",
        )

    prompt = _prompt_comparison_status(wilayah, negeri, swasta, pct_diff)
    narasi = await _call_narrator(
        prompt,
        fallback="",
        max_tokens=NARRATOR_TOKENS_COMPARISON,
    )

    if not narasi:
        return template_md

    # Validasi: angka negeri dan swasta wajib muncul
    if not _validate_numbers_in_narasi(narasi, {"negeri": negeri, "swasta": swasta}):
        logger.warning("[Narrator] Validasi angka gagal (comparison_status), pakai template")
        return template_md

    return f"{template_md}\n\n---\n\n{narasi}"


async def narrate_school_list(
    wilayah: str,
    total: int,
    schools: List[Dict[str, Any]],
    template_md: str,
    breakdown_jenjang: Optional[Dict[str, int]] = None,
    breakdown_status: Optional[Dict[str, int]] = None,
    active_filters: Optional[Dict[str, str]] = None,
    wilayah_level: str = "",
) -> str:
    """
    Narasi ringkasan untuk daftar sekolah.
    breakdown_jenjang & breakdown_status: dari ES sub-aggregation (opsional).
    Jika tidak tersedia, fallback ke hitung dari sampel schools.
    """
    if total == 0 or not schools:
        return _zero_result_message(wilayah, active_filters, modul="list")

    # Deteksi QC mode dari active_filters — tambahkan qc_summary ke prompt
    _qc_mode = bool(
        active_filters and (
            active_filters.get("qc_masalah_only")
            or active_filters.get("qc_field_list")
            or active_filters.get("qc_field")
        )
    )
    _qc_summary = _build_qc_summary_context(schools) if _qc_mode else None

    prompt = _prompt_school_list(
        wilayah, total, len(schools), schools,
        breakdown_jenjang, breakdown_status,
        active_filters,
        wilayah_level,
        qc_summary=_qc_summary,
    )
    narasi = await _call_narrator(
                                prompt,
                                fallback="",
                                max_tokens=NARRATOR_TOKENS_LIST
                                )

    if not narasi:
        return template_md

    # Validasi: total wajib ada di narasi
    if not _validate_numbers_in_narasi(narasi, {"total": total}):
        logger.warning("[Narrator] Validasi angka gagal (school_list), pakai template")
        return template_md

    return f"{template_md}\n\n---\n\n{narasi}"


async def narrate_school_profile(
    profile_data: Dict[str, Any],
    template_md: str,
) -> str:
    """Narasi profil sekolah. profile_data adalah dict hasil dari ES."""
    if not profile_data:
        return _zero_result_message(
            profile_data.get("nama", "sekolah yang dimaksud") if profile_data else "sekolah yang dimaksud",
            modul="profile",
        )

    # ── Debug log (hapus setelah field mapping terkonfirmasi) ────────────────
    # Set LOG_PROFILE_DEBUG=true di .env untuk aktifkan
    if os.getenv("LOG_PROFILE_DEBUG", "false").lower() == "true":
        logger.debug(
            "[PROFILE DEBUG] keys=%s",
            list(profile_data.keys())
        )
    # ────────────────────────────────────────────────────────────────────────

    prompt = _prompt_school_profile(profile_data)
    narasi = await _call_narrator(
                                prompt,
                                fallback="",
                                max_tokens=NARRATOR_TOKENS_PROFILE
                                )

    if not narasi:
        return template_md

    # Validasi angka kritis: guru dan siswa wajib ada di narasi
    f_check = _extract_profile_fields(profile_data)
    expected = {k: v for k, v in {
        "guru_total": f_check.get("guru_total") or None,
        "siswa":      f_check.get("siswa") or None,
    }.items() if v}
    if expected and not _validate_numbers_in_narasi(narasi, expected):
        logger.warning("[Narrator] Validasi angka gagal (profile), pakai template")
        return template_md

    # Validasi: nama sekolah wajib ada persis di narasi (tanpa diubah LLM)
    nama_sekolah = (profile_data or {}).get("nama", "")
    if nama_sekolah and nama_sekolah.lower() not in narasi.lower():
        logger.warning(
            "[Narrator] Nama sekolah '%s' tidak ditemukan di narasi, pakai template",
            nama_sekolah,
        )
        return template_md

    return f"{template_md}\n\n---\n\n{narasi}"


def narrate_qc_percentage(
    total_sekolah: int,
    total_bersih: int,
    total_bermasalah: int,
    persen_bersih: float,
    persen_bermasalah: float,
    wilayah_label: str = "seluruh Indonesia",
) -> str:
    """
    Bangun narasi persentase kualitas data sekolah.
    Menggunakan template statis (tidak perlu LLM — angkanya sudah pasti).
    """
    def _fmt(n: int) -> str:
        return f"{n:,}".replace(",", ".")

    persen_b = f"{persen_bersih:.1f}".replace(".", ",")
    persen_m = f"{persen_bermasalah:.1f}".replace(".", ",")

    return (
        f"Dari total {_fmt(total_sekolah)} sekolah di {wilayah_label}, "
        f"sebanyak {_fmt(total_bersih)} sekolah ({persen_b}%) memiliki data yang bersih. "
        f"Sementara {_fmt(total_bermasalah)} sekolah ({persen_m}%) masih memiliki "
        f"setidaknya satu masalah data yang perlu diperbaiki."
    )


async def narrate_out_of_context(query: str) -> str:
    """
    Jawab pertanyaan di luar konteks pendidikan.
    Berbeda dari fungsi lain: TIDAK menerima template_md karena
    tidak ada data ES yang mendahului — ini full-LLM response.
    Fallback statis jika LLM gagal.
    """
    prompt   = _prompt_ooc(query)
    fallback = (
        f'Pertanyaan "{query}" berada di luar cakupan sistem ini.\n\n'
        "Sistem ini dirancang khusus untuk data pendidikan dan persekolahan "
        "di Indonesia. Untuk pertanyaan umum, silakan gunakan sumber lain "
        "yang sesuai.\n\n"
        "Contoh pertanyaan yang dapat dijawab sistem ini: "
        '"Profil SMAN 1 Bogor", "Jumlah sekolah di Kabupaten Garut", '
        '"Daftar SMA negeri di Kecamatan Ciputat", '
        '"Bandingkan sekolah di Cibunar vs Lumpang Parungpanjang".'
    )
    return await _call_narrator(
                            prompt,
                            fallback=fallback,
                            max_tokens=NARRATOR_TOKENS_OOC
                            )

# ============================================================
# MULTI MODULE NARRATOR
# ============================================================

async def narrate_multi_module(
    query: str,
    merged_context: str,
    intent=None,
) -> str:
    """
    Menggabungkan merged_context string (dari orchestrator) menjadi 1 narasi terpadu.
    merged_context sudah berformat markdown dengan section dividers.
    """
    if not merged_context or not merged_context.strip():
        return "Tidak ada data yang dapat disajikan."

    # Kalau hanya 1 section (tidak ada "---" divider), skip LLM
    if "\n\n---\n\n" not in merged_context:
        return merged_context

    prompt = f"""
Gabungkan beberapa bagian informasi berikut menjadi jawaban yang ringkas, jelas, dan tidak berulang.

Pertahankan format markdown heading jika ada.

Pertanyaan user:
{query}

Konten:
{merged_context}

Instruksi:
- jangan menambahkan data baru
- jangan mengubah angka
- jangan membuat kesimpulan baru
- hanya rapikan alur kalimat
- pertahankan struktur markdown
"""

    narasi = await _call_narrator(
        prompt,
        fallback=merged_context,
        max_tokens=900,
    )

    expected_numbers = _extract_numbers_from_text(merged_context)
    if expected_numbers:
        if not _validate_numbers_in_narasi(narasi, expected_numbers):
            logger.warning("[Narrator] Validasi angka gagal (multi_module), pakai merged_context")
            return merged_context

    return narasi
