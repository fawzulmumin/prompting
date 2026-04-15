# school_vs_handler.py
"""
Handler untuk query perbandingan dua sekolah spesifik.
Contoh: "SMAN 3 Bandung vs SMAN 5 Bandung — mana yang lebih besar?"
"""
import re
import logging
from typing import Optional, Tuple, Dict, Any
from elasticsearch import AsyncElasticsearch

logger = logging.getLogger(__name__)

# Pattern nama sekolah spesifik dengan angka
_SCHOOL_NAME_RE = re.compile(
    r'\b(sd|mi|smp|mts|sma|smk|ma|sman|smpn|sdn|smkn|man|mtsn|min)'
    r'\s*(?:negeri|swasta)?\s*\d+\b',
    re.IGNORECASE
)


def is_school_vs_school_query(query: str) -> bool:
    """
    Deteksi apakah query adalah perbandingan dua nama sekolah spesifik.
    Harus ada 'vs' DAN dua nama sekolah dengan angka.
    """
    q = query.lower()
    if ' vs ' not in q:
        return False
    matches = _SCHOOL_NAME_RE.findall(q)
    return len(matches) >= 2


def extract_school_vs_parts(query: str) -> Optional[Tuple[str, str]]:
    """
    Ekstrak dua nama sekolah dari query vs.
    Return (school_a, school_b) atau None.
    """
    q = query.lower()
    parts = q.split(' vs ', 1)
    if len(parts) != 2:
        return None

    # Ambil bagian nama sekolah dari tiap sisi
    # Hapus kata-kata yang bukan bagian nama sekolah
    def clean_part(text: str) -> str:
        # Ambil hanya bagian yang relevan — nama sekolah biasanya di awal
        # Hapus pertanyaan di akhir ("mana yang lebih besar...", "di kecamatan mana...")
        stop_patterns = [
            r'\s*—.*$',          # "— mana yang lebih besar"
            r'\s*mana yang.*$',  # "mana yang lebih"
            r'\s*dan di.*$',     # "dan di kecamatan mana"
            r'\s*berapa.*$',     # "berapa siswanya"
        ]
        cleaned = text.strip()
        for pat in stop_patterns:
            cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE).strip()
        return cleaned.strip()

    left  = clean_part(parts[0])
    right = clean_part(parts[1])

    if not left or not right:
        return None

    logger.info("[SCHOOL_VS] Extracted: '%s' vs '%s'", left, right)
    return (left, right)


async def handle_school_vs_school(
    query: str,
    profile_handler,
) -> Optional[Dict[str, Any]]:
    """
    Cari profil dua sekolah dan kembalikan hasil perbandingan.
    Memanfaatkan profile_handler.handle() yang sudah ada.
    """
    parts = extract_school_vs_parts(query)
    if not parts:
        return None

    school_a_query, school_b_query = parts

    # Cari kedua sekolah secara parallel
    import asyncio
    result_a, result_b = await asyncio.gather(
        profile_handler.handle(school_a_query),
        profile_handler.handle(school_b_query),
        return_exceptions=True
    )

    # Handle exception
    if isinstance(result_a, Exception):
        logger.error("[SCHOOL_VS] Error searching school A: %s", result_a)
        result_a = {"status": "not_found"}
    if isinstance(result_b, Exception):
        logger.error("[SCHOOL_VS] Error searching school B: %s", result_b)
        result_b = {"status": "not_found"}

    # Kedua tidak ditemukan
    if result_a.get("status") != "success" and result_b.get("status") != "success":
        return None  # fallback ke profile handler normal

    return {
        "status": "success",
        "school_a": result_a,
        "school_b": result_b,
        "original_query": query,
    }

async def narrate_school_comparison(
    vs_result: Dict[str, Any],
    template_md: str,
) -> str:
    """
    Buat narasi perbandingan dua sekolah menggunakan DeepSeek.
    """
    from app.providers.deepseek import call_deepseek, DeepSeekUnavailableError

    school_a = vs_result.get("school_a", {})
    school_b = vs_result.get("school_b", {})

    def summarize(r: dict) -> str:
        if r.get("status") != "success":
            return "tidak ditemukan"
        data = r.get("data", {})
        identitas = data.get("identitas", {})
        lokasi    = data.get("lokasi", {})
        didik     = data.get("peserta_didik", {})
        ptk       = data.get("tenaga_pendidik", {})
        return (
            f"Nama: {identitas.get('nama', '-')}, "
            f"Status: {identitas.get('status', '-')}, "
            f"Akreditasi: {identitas.get('akreditasi', '-')}, "
            f"Kecamatan: {lokasi.get('kecamatan', '-')}, "
            f"Kabupaten: {lokasi.get('kabupaten', '-')}, "
            f"Total Siswa: {didik.get('total_siswa', '-')}, "
            f"Total PTK: {ptk.get('total_ptk', '-')}"
        )

    prompt = f"""Bandingkan dua sekolah berikut secara ringkas dalam 3-4 kalimat.
Fokus pada: jumlah siswa, kecamatan/lokasi, dan akreditasi.
Sebutkan mana yang lebih besar dan di mana masing-masing berada.
JANGAN mengarang data — gunakan HANYA data yang diberikan.

Sekolah A: {summarize(school_a)}
Sekolah B: {summarize(school_b)}

Narasi perbandingan:"""

    try:
        narasi = await call_deepseek(prompt, max_tokens=300, temperature=0.3)
        if narasi:
            return f"{template_md}\n\n---\n\n{narasi}"
    except (DeepSeekUnavailableError, Exception) as e:
        logger.warning("[SCHOOL_VS] Narasi gagal: %s", e)

    return template_md

def format_school_comparison_markdown(result: Dict[str, Any]) -> str:
    """
    Format hasil perbandingan dua sekolah ke markdown.
    """
    school_a = result.get("school_a", {})
    school_b = result.get("school_b", {})

    def extract_info(r: dict) -> dict:
        if r.get("status") != "success":
            return {}
        data = r.get("data", {})
        identitas = data.get("identitas", {})
        lokasi    = data.get("lokasi", {})
        didik     = data.get("peserta_didik", {})
        ptk       = data.get("tenaga_pendidik", {})
        return {
            "nama":        identitas.get("nama") or "-",         # ← "nama" bukan "nama_sekolah"
            "npsn":        identitas.get("npsn") or "-",
            "status":      identitas.get("status") or "-",       # ← "status" bukan "status_sekolah"
            "akreditasi":  identitas.get("akreditasi") or "-",
            "kecamatan":   lokasi.get("kecamatan") or "-",
            "kabupaten":   lokasi.get("kabupaten") or "-",       # ← "kabupaten" bukan "kabupaten_kota"
            "total_siswa": didik.get("total_siswa") or "-",      # ← sama, sudah benar
            "total_ptk":   ptk.get("total_ptk") or "-",
        }

    a = extract_info(school_a)
    b = extract_info(school_b)

    # Tentukan mana yang lebih besar (siswa)
    winner_label = ""
    try:
        siswa_a = int(str(a.get("total_siswa", 0)).replace(",", ""))
        siswa_b = int(str(b.get("total_siswa", 0)).replace(",", ""))
        if siswa_a > siswa_b:
            winner_label = f"\n\n**{a['nama']}** lebih besar dari sisi jumlah siswa " \
                           f"({siswa_a:,} vs {siswa_b:,} siswa)."
        elif siswa_b > siswa_a:
            winner_label = f"\n\n**{b['nama']}** lebih besar dari sisi jumlah siswa " \
                           f"({siswa_b:,} vs {siswa_a:,} siswa)."
        else:
            winner_label = "\n\nKedua sekolah memiliki jumlah siswa yang sama."
    except (ValueError, TypeError):
        pass

    def row(label, val_a, val_b):
        return f"| {label} | {val_a} | {val_b} |"

    nama_a = a.get("nama", "Sekolah A") if a else "Tidak ditemukan"
    nama_b = b.get("nama", "Sekolah B") if b else "Tidak ditemukan"

    lines = [
        "### Perbandingan Sekolah",
        "",
        f"| Aspek | {nama_a} | {nama_b} |",
        "|---|---|---|",
    ]

    if a and b:
        lines += [
            row("NPSN",          a.get("npsn", "-"),           b.get("npsn", "-")),
            row("Status",        a.get("status", "-"),         b.get("status", "-")),
            row("Akreditasi",    a.get("akreditasi", "-"),      b.get("akreditasi", "-")),
            row("Kecamatan",     a.get("kecamatan", "-"),       b.get("kecamatan", "-")),
            row("Kabupaten/Kota", a.get("kabupaten", "-"),      b.get("kabupaten", "-")),
            row("Total Siswa",   a.get("total_siswa", "-"),     b.get("total_siswa", "-")),
            row("Total PTK",     a.get("total_ptk", "-"),       b.get("total_ptk", "-")),
        ]
    elif a:
        lines.append(f"| Data lengkap | tersedia | tidak ditemukan |")
    elif b:
        lines.append(f"| Data lengkap | tidak ditemukan | tersedia |")

    lines.append(winner_label)

    return "\n".join(lines)
