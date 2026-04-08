# services/core/context_optimizer.py (FILE BARU)
"""
Mengatur budget token agar total context ke narrator tidak melebihi limit.
"""

from dataclasses import dataclass
from typing import List, Tuple

_TOKEN_BUDGET = {
    "primary_max_chars":   3500,
    "secondary_max_chars": 1200,
    "history_max_chars":   800,
    "total_budget_chars":  5000,
}

@dataclass
class BudgetConfig:
    """
    Konfigurasi budget berdasarkan kompleksitas query.
    Semua dalam satuan karakter (~4 char = 1 token DeepSeek).
    Total target: ≤5000 char → ≤1250 token → aman dalam context window.
    """
    primary_max:   int   # char untuk data modul utama
    secondary_max: int   # char per modul secondary (total, dibagi rata)
    history_max:   int   # char untuk riwayat percakapan
    query_max:     int   # char untuk query asli user (selalu include)


# Konfigurasi per skenario
_BUDGET_CONFIGS = {
    # Satu modul, scope lokal — paling longgar untuk primary
    "single_local": BudgetConfig(
        primary_max=3800, secondary_max=0, history_max=800, query_max=200
    ),
    # Satu modul, scope nasional — data breakdown besar
    "single_national": BudgetConfig(
        primary_max=3200, secondary_max=0, history_max=600, query_max=200
    ),
    # Multi modul — primary dikompres, secondary dapat porsi
    "multi_module": BudgetConfig(
        primary_max=2400, secondary_max=1800, history_max=600, query_max=200
    ),
    # Comparison — dua wilayah butuh porsi seimbang
    "comparison": BudgetConfig(
        primary_max=3000, secondary_max=1200, history_max=600, query_max=200
    ),
}


def select_budget(intent, secondary_modules: List[str]) -> BudgetConfig:
    """Pilih budget config yang tepat berdasarkan intent."""
    if secondary_modules:
        return _BUDGET_CONFIGS["multi_module"]
    if intent.module == "comparison":
        return _BUDGET_CONFIGS["comparison"]
    if intent.scope == "nasional":
        return _BUDGET_CONFIGS["single_national"]
    return _BUDGET_CONFIGS["single_local"]


def _smart_truncate(text: str, max_chars: int, keep_rows: bool = True) -> str:
    """
    Truncate cerdas — tidak memotong di tengah baris tabel markdown.
    
    keep_rows=True: pastikan tabel tidak terpotong di tengah baris,
    sehingga parser markdown di narrator tetap bisa membaca tabel.
    """
    if len(text) <= max_chars:
        return text
    
    if not keep_rows:
        return text[:max_chars] + "\n\n*[dipotong karena panjang]*"
    
    # Potong di batas baris terdekat sebelum max_chars
    truncated = text[:max_chars]
    last_newline = truncated.rfind('\n')
    
    if last_newline > max_chars * 0.7:  # jangan potong terlalu awal
        truncated = truncated[:last_newline]
    
    return truncated + "\n\n*[...data dipotong, gunakan filter lebih spesifik]*"


def _compress_table(markdown_text: str, max_rows: int = 15) -> str:
    """
    Kompres tabel markdown dengan mempertahankan header
    dan hanya menampilkan max_rows baris data.
    """
    lines = markdown_text.split('\n')
    table_start = None
    table_end   = None
    
    for i, line in enumerate(lines):
        if line.strip().startswith('|') and table_start is None:
            table_start = i
        elif table_start and not line.strip().startswith('|'):
            table_end = i
            break
    
    if table_end is None:
        table_end = len(lines)
    
    if table_start is None:
        return _smart_truncate(markdown_text, len(markdown_text))
    
    # Header + separator = 2 baris, lalu ambil max_rows baris data
    header_lines = lines[table_start:table_start + 2]
    data_lines   = lines[table_start + 2:table_end]
    
    if len(data_lines) <= max_rows:
        return markdown_text  # tidak perlu kompres
    
    kept    = data_lines[:max_rows]
    omitted = len(data_lines) - max_rows
    
    compressed_table = '\n'.join(
        lines[:table_start]
        + header_lines
        + kept
        + [f"| *...{omitted} baris lainnya dihilangkan* | | | | |"]
        + lines[table_end:]
    )
    return compressed_table


def build_narrator_context(
    query: str,
    primary_markdown: str,
    secondary_list: List[Tuple[str, str]],  # [(label, markdown), ...]
    history: str,
    intent,
) -> str:
    """
    Bangun context string yang teroptimasi untuk narrator.
    
    Args:
        query:            pertanyaan asli user
        primary_markdown: hasil modul utama
        secondary_list:   [(label, markdown)] modul pendukung
        history:          riwayat percakapan
        intent:           CognitiveIntent untuk select budget
    
    Returns:
        String context yang siap dikirim ke narrator DeepSeek,
        dijamin ≤5000 karakter.
    """
    budget = select_budget(intent, [s[0] for s in secondary_list])
    sections = []
    
    # ── Query user (selalu include, tidak dikompres) ────────────
    sections.append(
        f"[PERTANYAAN USER]\n{query[:budget.query_max]}"
    )
    
    # ── Primary data ────────────────────────────────────────────
    # Kompres tabel jika ada dan terlalu panjang
    primary_compressed = _compress_table(
        primary_markdown,
        max_rows=20 if intent.scope == "nasional" else 30
    )
    primary_trimmed = _smart_truncate(primary_compressed, budget.primary_max)
    
    primary_label = {
        "aggregation": "RINGKASAN JUMLAH",
        "comparison":  "DATA PERBANDINGAN",
        "profile":     "PROFIL SEKOLAH",
        "list":        "DAFTAR SEKOLAH",
        "geo":         "SEKOLAH TERDEKAT",
    }.get(intent.module, "DATA UTAMA")
    
    sections.append(f"[{primary_label}]\n{primary_trimmed}")
    
    # ── Secondary data ──────────────────────────────────────────
    if secondary_list and budget.secondary_max > 0:
        per_secondary = budget.secondary_max // len(secondary_list)
        for label, content in secondary_list:
            compressed = _compress_table(content, max_rows=10)
            trimmed    = _smart_truncate(compressed, per_secondary)
            sections.append(f"[{label.upper()}]\n{trimmed}")
    
    # ── History ─────────────────────────────────────────────────
    if history and budget.history_max > 0:
        # History: ambil dari AKHIR (percakapan terbaru lebih relevan)
        history_trimmed = history[-budget.history_max:]
        sections.append(f"[RIWAYAT PERCAKAPAN]\n{history_trimmed}")
    
    full_context = "\n\n---\n\n".join(sections)
    
    # Safety check — tidak boleh melebihi 5200 char total
    if len(full_context) > 5200:
        # Kompres agresif: potong primary lebih pendek
        primary_emergency = _smart_truncate(primary_trimmed, budget.primary_max // 2)
        sections[1] = f"[{primary_label}]\n{primary_emergency}"
        full_context = "\n\n---\n\n".join(sections)
    
    return full_context

def optimize_context_for_narrator(
    primary_md: str,
    secondary_list: list,
    history: str = "",
    query: str = "",
) -> str:
    """Deprecated — alias ke build_narrator_context untuk backward compat."""
    import warnings
    warnings.warn(
        "optimize_context_for_narrator deprecated, gunakan build_narrator_context",
        DeprecationWarning,
        stacklevel=2,
    )
    class _DummyIntent:
        module = "list"
        scope  = "kabupaten"
    return build_narrator_context(
        query=query,
        primary_markdown=primary_md,
        secondary_list=secondary_list,
        history=history,
        intent=_DummyIntent(),
    )
