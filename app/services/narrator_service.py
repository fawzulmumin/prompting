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
 
import logging
import os
from typing import Dict, Any, List, Optional
 
from app.providers.deepseek import call_deepseek, DeepSeekUnavailableError
 
logger = logging.getLogger(__name__)
 
# Timeout khusus narasi — lebih pendek dari RAG flow
# ── TIMEOUT CONFIG ──
NARRATOR_TIMEOUT             = 25
NARRATOR_TIMEOUT_AGGREGATION = 25

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

def _validate_numbers_in_narasi(narasi: str, expected_numbers: dict) -> bool:
    """
    Pastikan setiap angka kritis dari data asli masih ada di teks narasi.
    Menerima format: 1234, 1.234, 1,234 (pemisah ribuan berbeda-beda).

    Jika angka kritis hilang atau berubah → return False → pakai template fallback.

    Parameters
    ----------
    narasi          : teks hasil LLM
    expected_numbers: {label: int/float} — angka yang WAJIB ada di narasi
    """
    import re as _re

    def _normalise(text: str) -> str:
        # Hapus semua pemisah ribuan (titik, koma, spasi) agar bisa di-compare
        return _re.sub(r"[.,\s]", "", text)

    narasi_norm = _normalise(narasi)

    for label, val in expected_numbers.items():
        if val is None:
            continue
        # Cek berbagai representasi: 143, 1.432, 1,432, 1 432
        val_str = _normalise(str(int(val)))
        if val_str not in narasi_norm:
            logger.warning(
                "[Narrator] Angka kritis hilang dari narasi — label=%s expected=%s",
                label, val
            )
            return False
    return True


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
            f"  {i+1}. {item['name'].title()}: {item['count']:,} sekolah"
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
                f"Sebutkan top 3 {drilldown_label} dengan jumlah sekolah "
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
            f"Kalimat 1: Sebutkan PERSIS angka {total:,} sekolah di {wilayah} "
            f"({level_label}). Jangan bulatkan atau ubah angka ini.\n"
            "Kalimat 2: Bandingkan dengan benchmark — gunakan label konteks yang "
            "sudah disediakan (banyak/sedang/sedikit).\n"
            "Kalimat 3: Satu implikasi praktis bagi perencanaan pendidikan di wilayah ini.\n"
            f"LARANGAN KERAS: Jangan tulis angka selain {total:,} untuk jumlah sekolah."
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

    return f"""{_SYSTEM_BASE}

DATA AGREGASI:
- Wilayah: {wilayah} (level: {level_label})
- Jumlah sekolah{status_info}: {total:,}{filter_section}{pct_section}
- Konteks: jumlah ini {konteks_bench}{breakdown_section}{drilldown_section}

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
 
    school_lines = "\n".join(
        f"- {s.get('nama', 'Nama tidak tersedia')} "
        f"({s.get('kecamatan', '-')})"
        for s in schools_list[:20]  # batasi 20 untuk context
    )
 
    breakdown_str = ""
    if breakdown_jenjang:
        top = sorted(breakdown_jenjang.items(), key=lambda x: -x[1])[:5]
        breakdown_str = "Breakdown jenjang: " + ", ".join(f"{k}: {v:,}" for k, v in top)
 
    return (
        f"{_SYSTEM_BASE}\n\n"
        "TUGAS: Buat narasi yang menggabungkan dua informasi berikut:\n\n"
        f"DATA AGGREGASI:\n"
        f"- Wilayah: {wilayah}\n"
        f"- Filter: {filter_str}\n"
        f"- Total satuan pendidikan: {total:,}\n"
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
                f"\n\nDATA RATA-RATA SISWA PER SEKOLAH"
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
            f"Kecamatan: {s.get('kecamatan','-')} | Siswa: {s.get('siswa_total',0):,} | "
            f"Guru: {s.get('guru_total',0)}"
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
        siswa    = f"{s.get('siswa_total', 0):,}" if s.get('siswa_total') else '-'
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
    return f"""{_SYSTEM_BASE}

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
    Normalisasi profile_data ke field flat.
 
    Format aktual yang datang dari profile_formatter.py (terkonfirmasi via debug log):
        keys = ['identitas', 'lokasi', 'sarana_prasarana',
                'tenaga_pendidik', 'peserta_didik', 'legalitas']
 
    Sub-dict 'tenaga_pendidik' berisi: total_ptk, ptk_laki_laki, ptk_perempuan,
        guru_pns, guru_honor, guru_sertifikasi
    Sub-dict 'peserta_didik'   berisi: total_siswa
    Sub-dict 'identitas'       berisi: nama_sekolah, npsn, status_sekolah,
        akreditasi, bentuk_pendidikan
    Sub-dict 'lokasi'          berisi: alamat_jalan, kecamatan, kabupaten_kota, provinsi
    Sub-dict 'sarana_prasarana' berisi: akses_internet, sumber_listrik, sumber_air
 
    Juga mendukung format flat ES langsung (fallback).
    """
    def _get(keys: list, default=None):
        """
        Coba key satu per satu.
        Format "sub_dict.field" → profile_data[sub_dict][field]
        """
        for k in keys:
            if "." in k:
                parts = k.split(".", 1)
                sub = profile_data.get(parts[0], {})
                v = sub.get(parts[1]) if isinstance(sub, dict) else None
            else:
                v = profile_data.get(k)
            # Nilai kosong / dash / None dianggap tidak ada
            if v is not None and v != "" and v != "-":
                return v
        return default
 
    def _int(keys: list) -> int:
        v = _get(keys, 0)
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return 0
 
    # ── PTK — prioritas: nested formatter → flat ES → fallback ───────────────
    guru_total = _int([
        "tenaga_pendidik.total_ptk",
        "jumlah_ptk", "total_ptk", "total_guru",
    ])
    guru_pns = _int([
        "tenaga_pendidik.guru_pns",
        "jumlah_guru_pns", "guru_pns",
    ])
    guru_honor = _int([
        "tenaga_pendidik.guru_honor",
        "jumlah_guru_honor", "guru_honor",
    ])
    guru_serti = _int([
        "tenaga_pendidik.guru_sertifikasi",
        "jumlah_guru_sertifikasi", "guru_sertifikasi",
    ])
    siswa_total = _int([
        "peserta_didik.total_siswa",
        "jumlah_siswa_total", "total_siswa", "jumlah_siswa",
    ])
 
    # Fallback: jika total_ptk=0 tapi pns+honor ada → pakai sum
    if guru_total == 0 and (guru_pns + guru_honor) > 0:
        guru_total = guru_pns + guru_honor
 
    return {
        "nama": _get([
            "identitas.nama",           # ← BENAR
            "identitas.nama_sekolah",
            "nama", "nama_sekolah",
        ], "Tidak diketahui"),
        "npsn": str(_get([
            "identitas.npsn", "npsn",
        ], "-")),
        "bentuk": _get([
            "identitas.bentuk_pendidikan",
            "bentuk_pendidikan", "bentuk",
        ], "-"),
        "status": _get([
            "identitas.status",         # ← BENAR
            "identitas.status_sekolah",
            "status_sekolah", "status",
        ], "-"),
        "akreditasi": _get([
            "identitas.akreditasi", "akreditasi",
        ], "-"),
        "alamat": _get([
            "lokasi.alamat",            # ← BENAR
            "lokasi.alamat_jalan",
            "alamat", "alamat_jalan",
        ], ""),
        "kec": _get([
            "lokasi.kecamatan",         # ← sudah benar
            "kecamatan",
        ], ""),
        "kab": _get([
            "lokasi.kabupaten",         # ← BENAR (bukan kabupaten_kota)
            "lokasi.kabupaten_kota",
            "kabupaten", "kabupaten_kota",
        ], ""),
        "prov": _get([
            "lokasi.provinsi", "provinsi",
        ], ""),
        "guru_total": guru_total,
        "guru_pns":   guru_pns,
        "guru_honor": guru_honor,
        "guru_serti": guru_serti,
        "siswa":      siswa_total,
        "internet": _get([
            "sarana_prasarana.akses_internet",   # ← sudah benar
            "akses_internet", "internet",
        ], "-"),
        "listrik": _get([
            "sarana_prasarana.sumber_listrik",   # ← sudah benar
            "sumber_listrik", "listrik",
        ], "-"),
        "air": _get([
            "sarana_prasarana.sumber_air",       # ← sudah benar
            "sumber_air", "air",
        ], "-"),
    }
 
 
def _prompt_school_profile(profile_data: Dict[str, Any]) -> str:
    f          = _extract_profile_fields(profile_data)
    nama       = f["nama"]
    npsn       = f["npsn"]
    bentuk     = f["bentuk"]
    status     = f["status"]
    akreditasi = f["akreditasi"]
    lokasi_str = ", ".join(filter(None, [f["kec"], f["kab"], f["prov"]])) or "-"

    guru_total  = f["guru_total"]
    guru_pns    = f["guru_pns"]
    guru_honor  = f["guru_honor"]
    guru_serti  = f["guru_serti"]
    siswa_total = f["siswa"]
    internet    = f["internet"]
    listrik     = f["listrik"]
    air         = f["air"]

    rasio_str = (
        f"1:{round(siswa_total / guru_total)}"
        if guru_total and siswa_total else "tidak dapat dihitung"
    )
    pct_serti = (
        f"{round(guru_serti / guru_total * 100)}%"
        if guru_total else "-"
    )

    # Benchmark nasional (pendekatan)
    RASIO_IDEAL = {"SD": 17, "SMP": 20, "SMA": 20, "SMK": 15}
    rasio_ideal = RASIO_IDEAL.get(bentuk, 20)
    rasio_angka = round(siswa_total / guru_total) if guru_total and siswa_total else None
    if rasio_angka:
        if rasio_angka <= rasio_ideal:
            rasio_konteks = f"lebih baik dari rata-rata nasional {bentuk} (~1:{rasio_ideal})"
        elif rasio_angka <= rasio_ideal * 1.5:
            rasio_konteks = f"sedikit di atas rata-rata nasional {bentuk} (~1:{rasio_ideal})"
        else:
            rasio_konteks = f"jauh di atas rata-rata nasional {bentuk} (~1:{rasio_ideal}), menunjukkan beban mengajar yang tinggi"
    else:
        rasio_konteks = ""

    # Identifikasi kondisi kritis
    catatan_kritis = []
    if guru_total == 0:
        catatan_kritis.append("DATA PTK BELUM DILAPORKAN — jangan simpulkan tidak ada guru")
    if internet in ("Tidak Ada", "-", "", None):
        catatan_kritis.append("tidak ada akses internet")
    if listrik in ("Tidak Ada", "-", "", None):
        catatan_kritis.append("sumber listrik tidak tercatat")
    if air in ("Air hujan", ""):
        catatan_kritis.append(f"sumber air: {air} (perlu perhatian)")

    catatan_str = "; ".join(catatan_kritis) if catatan_kritis else "tidak ada catatan kritis"

    # Konteks akreditasi
    akred_konteks = {
        "A": "akreditasi terbaik — menunjukkan standar mutu yang tinggi",
        "B": "akreditasi baik — masih ada ruang peningkatan menuju A",
        "C": "akreditasi cukup — perlu peningkatan di beberapa aspek",
        "-": "belum terakreditasi atau data tidak tersedia",
    }.get(akreditasi, "")

    return f"""{_SYSTEM_BASE}

DATA SEKOLAH:
- Nama       : {nama}
- Jenjang    : {bentuk} | Status: {status} | Akreditasi: {akreditasi} ({akred_konteks})
- Lokasi     : {lokasi_str}
- Total PTK  : {guru_total} (PNS: {guru_pns}, Honor: {guru_honor}, Sertifikasi: {guru_serti} / {pct_serti})
- Total Siswa: {siswa_total:,} | Rasio guru:siswa ≈ {rasio_str} — {rasio_konteks}
- Internet   : {internet} | Listrik: {listrik} | Air: {air}
- Catatan    : {catatan_str}

INSTRUKSI NARASI (4 paragraf pendek dengan ## heading, TANPA bullet):

## Identitas & Posisi
Sebutkan nama sekolah PERSIS "{nama}" (jangan diubah sama sekali).
Sebutkan bahwa ini adalah {bentuk} berstatus {status} yang berlokasi di {lokasi_str}.
Jika status="-": tulis "status belum tercatat". Jangan menebak.
Interpretasikan akreditasi {akreditasi}: {akred_konteks}.

## Kapasitas & Tenaga Pengajar
Sebutkan jumlah siswa. Lalu bahas PTK:
- Jika PTK=0: sampaikan data belum tercatat, JANGAN simpulkan tidak ada guru.
- Jika PTK>0: interpretasikan rasio guru:siswa dengan konteks benchmark nasional
  yang sudah dihitung di atas. Sebutkan persentase sertifikasi dan apa artinya.

## Komposisi & Stabilitas Guru
(Hanya jika PTK>0) Interpretasikan proporsi PNS vs honorer.
Jika honorer > PNS: sorot potensi dampak pada stabilitas jangka panjang.
Jika PNS dominan: sorot stabilitas yang baik.

## Sarana & Rekomendasi
Komentari kondisi internet, listrik, air secara jujur.
Jika ada kondisi kritis (dari catatan di atas): SOROT sebagai temuan penting,
bukan sekadar disebutkan. Tutup dengan satu kalimat insight keseluruhan.

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
        result = await call_deepseek(
            prompt=prompt,
            temperature=NARRATOR_TEMP,
            max_tokens=max_tokens,
            timeout=timeout,  # ✅ sekarang pakai parameter
        )
        result = result.strip()

        # strip prefix kalau LLM nambahin label
        for prefix in ("NARASI:", "JAWABAN:", "narasi:", "jawaban:"):
            if result.upper().startswith(prefix.upper()):
                result = result[len(prefix):].strip()
                break

        return result if result else fallback

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
) -> str:
    if total == 0:
        return _zero_result_message(wilayah, active_filters, modul="aggregation")

    prompt = _prompt_aggregation(
        wilayah, total, level, status_filter,
        breakdown_jenjang, breakdown_status,
        drilldown_top3, drilldown_field,
        total_all=total_all,           
        pct_of_total=pct_of_total,     
        active_filters=active_filters, 
        jenjang_requested=jenjang_requested,
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
    if not _validate_numbers_in_narasi(narasi, expected):
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
 
    prompt = _prompt_school_list(
        wilayah, total, len(schools), schools,
        breakdown_jenjang, breakdown_status,
        active_filters,
        wilayah_level,
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

    # Validasi: nama sekolah wajib ada persis di narasi (tanpa diubah LLM)
    nama_sekolah = (profile_data or {}).get("nama", "")
    if nama_sekolah and nama_sekolah.lower() not in narasi.lower():
        logger.warning(
            "[Narrator] Nama sekolah '%s' tidak ditemukan di narasi, pakai template",
            nama_sekolah,
        )
        return template_md

    return f"{template_md}\n\n---\n\n{narasi}"


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
    module_outputs: List[Dict[str, Any]],
    template_md: str
) -> str:
    """
    Menggabungkan output beberapa modul menjadi 1 narasi terpadu.
    
    module_outputs contoh:
    [
        {
            "module": "aggregation",
            "content_md": "### Jumlah Sekolah\n..."
        },
        {
            "module": "school_list",
            "content_md": "### Daftar Sekolah\n..."
        }
    ]
    """

    # fallback jika kosong
    if not module_outputs:
        return template_md

    # gabungkan markdown dasar
    combined_sections = []
    for mod in module_outputs:
        content = mod.get("content_md") or mod.get("content") or ""
        if content:
            combined_sections.append(content.strip())

    merged_md = "\n\n".join(combined_sections)

    # jika hanya 1 modul, tidak perlu LLM
    if len(module_outputs) == 1:
        return merged_md

    # prompt LLM untuk merapikan narasi gabungan
    prompt = f"""
Gabungkan beberapa bagian informasi berikut menjadi jawaban yang ringkas, jelas, dan tidak berulang.

Pertahankan format markdown heading jika ada.

Pertanyaan user:
{query}

Konten:
{merged_md}

Instruksi:
- jangan menambahkan data baru
- jangan mengubah angka
- jangan membuat kesimpulan baru
- hanya rapikan alur kalimat
- pertahankan struktur markdown
"""

    fallback = merged_md

    return await _call_narrator(
        prompt,
        fallback,
        max_tokens=900,
        timeout=NARRATOR_TIMEOUT_AGGREGATION
    )
