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
#   worst-case = TIMEOUT + 1 + TIMEOUT ≈ 35s (simple) / 41s (aggregation with breakdown)
# asyncio.wait_for di _call_narrator memastikan hard-cap tidak terlampaui.
# Grace period +5s antara call_deepseek(timeout=T) dan wait_for(timeout=T+5)
# mencegah race condition: asyncio membunuh coroutine SEBELUM tenacity sempat retry.
# call_deepseek meneruskan timeout sebagai httpx read timeout — tidak perlu ubah deepseek.py.
# ── TIMEOUT CONFIG ──
NARRATOR_TIMEOUT             = 60   # simple narasi — hard cap 60s (naik dari 35s)
NARRATOR_TIMEOUT_AGGREGATION = 65   # aggregation + breakdown — lebih longgar (naik dari 40s)
_NARRATOR_WAIT_FOR_GRACE     = 5    # detik ekstra untuk asyncio.wait_for di atas timeout LLM

# ── TOKEN CONFIG ──
NARRATOR_TOKENS_PROFILE      = 1500
NARRATOR_TOKENS_AGGREGATION  = 1400
NARRATOR_TOKENS_COMPARISON   = 1000
NARRATOR_TOKENS_LIST         = 2000  # Dinaikkan untuk list dengan banyak masalah per sekolah
NARRATOR_TOKENS_OOC          = 800

# ── TEMP CONFIG ──
NARRATOR_TEMP                = 0.4


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT BUILDERS (di narrator_prompts.py)
# ─────────────────────────────────────────────────────────────────────────────
from app.services.formatters.narrator_prompts import (  # noqa: E402
    _SYSTEM_BASE,
    _benchmark_label,
    _safe,
    _build_wilayah_display_name,
    _build_school_table,
    _extract_profile_fields,
    QC_CONTEXT_LIBRARY,
    _prompt_aggregation,
    _prompt_aggregation_with_list,
    _prompt_ooc_guardrail,
    _prompt_comparison_lokasi,
    _prompt_school_vs_school,
    _prompt_comparison_with_list,
    _prompt_comparison_status,
    _prompt_school_list,
    _prompt_school_profile,
    _prompt_ooc,
    build_multi_metric_aggregation_prompt,
)


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATOR — Cek angka kritis tidak berubah setelah narasi LLM
# ─────────────────────────────────────────────────────────────────────────────

def _validate_numbers_in_narasi(narasi: str, expected_numbers) -> Optional[str]:
    """
    Return narasi jika semua angka kritis ada di teks.
    Return None jika ada angka kritis yang hilang.

    Menerima variasi format angka Indonesia:
    - 4414, 4.414, 4,414, 4 414 (semua separator ribuan)
    - Angka dibulatkan (4.400 untuk 4.414) — toleransi ±2%
    """
    import re as _re

    def _normalize(text: str) -> str:
        return _re.sub(r"[.,\s]", "", text)

    def _normalize_for_validation(text: str) -> str:
        """Hapus separator ribuan umum (titik, koma, spasi) sebelum normalisasi penuh."""
        # titik sebagai separator ribuan: 1.234 → 1234
        t = _re.sub(r'(\d)\.(\d{3})(?!\d)', r'\1\2', text)
        # koma sebagai separator ribuan: 1,234 → 1234
        t = _re.sub(r'(\d),(\d{3})(?!\d)', r'\1\2', t)
        # spasi sebagai separator ribuan: 1 234 → 1234
        t = _re.sub(r'(\d) (\d{3})(?!\d)', r'\1\2', t)
        return t

    narasi_normalized = _normalize(_normalize_for_validation(narasi))

    if isinstance(expected_numbers, dict):
        items = list(expected_numbers.items())
    else:
        items = [(str(v), v) for v in expected_numbers]

    for label, val in items:
        if val is None:
            continue

        val_int = int(val)
        val_str = _normalize(str(val_int))

        # Cek exact match (setelah normalisasi)
        if val_str in narasi_normalized:
            continue

        # Toleransi ±2% untuk pembulatan LLM (khusus angka besar > 100)
        if val_int > 100:
            tolerance = max(1, int(val_int * 0.02))
            found = any(
                _normalize(str(v)) in narasi_normalized
                for v in range(val_int - tolerance, val_int + tolerance + 1)
            )
            if found:
                continue

        logger.warning(
            "[Narrator] Angka kritis hilang dari narasi — label=%s expected=%s",
            label, val
        )
        return None

    return narasi


def _enrich_template_on_validation_fail(
    template_md: str,
    expected: dict,
    context: str = "",
) -> str:
    """
    Tambahkan catatan ringkas ke template saat LLM gagal mempertahankan angka.
    Deterministik (tidak pakai LLM), lebih informatif dari bare template.
    """
    if not expected:
        return template_md
    angka_str = ", ".join(
        f"{label}: {int(val):,}".replace(",", ".")
        for label, val in expected.items()
        if val is not None
    )
    catatan = f"\n\n> _Data ringkas: {angka_str}._" if angka_str else ""
    return f"{template_md}{catatan}"


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


def _build_geo_qc_note(schools: list, max_schools: int = 3) -> str:
    """
    Ringkasan QC untuk geo search — sebutkan max 3 sekolah dengan total_masalah > 0.
    Gunakan QC_CONTEXT_LIBRARY untuk nama field yang mudah dibaca.
    Return "" jika semua sekolah bersih.
    """
    bermasalah = [s for s in schools if (s.get("total_masalah") or 0) > 0]
    if not bermasalah:
        return ""

    lines = []
    for s in bermasalah[:max_schools]:
        nama  = s.get("nama", "?")
        total = s.get("total_masalah", 0)
        keys  = s.get("masalah_keys") or []
        deskripsi = [
            QC_CONTEXT_LIBRARY[k]["nama"]
            for k in keys
            if k in QC_CONTEXT_LIBRARY
        ]
        daftar = ", ".join(deskripsi) if deskripsi else "masalah tidak teridentifikasi"
        lines.append(f"- {nama}: {total} masalah ({daftar})")

    return "CATATAN QC SEKOLAH:\n" + "\n".join(lines)


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
    for lvl in ("desa_kelurahan", "kecamatan", "kabupaten", "provinsi", "pulau"):
        if entities.get(lvl):
            return lvl
    return "nasional"


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
    distribusi: Optional[list] = None,
    distribusi_field: Optional[str] = None,
    qc_detail: Optional[Dict] = None,
    numeric_filters: Optional[List] = None,
) -> str:
    if total == 0:
        return _zero_result_message(wilayah, active_filters, modul="aggregation")

    # ── Fast path: simple query tanpa breakdown/drilldown ────────────────────
    # Skip fast path jika ada numeric_filters — LLM perlu menyebut kondisi filter.
    _is_simple = not (breakdown_jenjang or breakdown_status or drilldown_top3)
    _is_metric = bool(metric_target and metric_target not in (None, "sekolah"))
    _has_numeric_filter = bool(numeric_filters)
    if _is_simple and not _is_metric and not _has_numeric_filter:
        from app.services.core.local_narrator import narrate_aggregation_simple
        _jenjang = (active_filters or {}).get("bentuk_pendidikan")
        _status  = (active_filters or {}).get("status_sekolah") or status_filter
        fast = narrate_aggregation_simple(wilayah, total, level, _jenjang, _status, metric_target)
        if fast:
            return fast

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
    metric_label = _METRIC_LABELS.get(metric_target or "sekolah", metric_target or "sekolah")

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
        distribusi=distribusi,
        distribusi_field=distribusi_field,
        qc_detail=qc_detail,
        numeric_filters=numeric_filters,
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
        logger.warning(
            "[Narrator] Validasi angka gagal (aggregation) — expected=%s, pakai template",
            expected,
        )
        return _enrich_template_on_validation_fail(template_md, expected, context="aggregation")

    return narasi


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
        logger.warning(
            "[Narrator] Validasi angka gagal (comparison_lokasi) — expected=%s, pakai template",
            expected,
        )
        return _enrich_template_on_validation_fail(template_md, expected, context="comparison_lokasi")

    return narasi


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
        logger.warning(
            "[Narrator] Validasi angka gagal (comparison_status) — negeri=%s swasta=%s, pakai template",
            negeri, swasta,
        )
        return _enrich_template_on_validation_fail(
            template_md, {"negeri": negeri, "swasta": swasta}, context="comparison_status"
        )

    return narasi


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

    # Geo QC: sekolah dengan total_masalah > 0 (bukan mode QC residu)
    _geo_qc_note = (
        _build_geo_qc_note(schools)
        if not _qc_mode and any((s.get("total_masalah") or 0) > 0 for s in schools)
        else None
    )

    prompt = _prompt_school_list(
        wilayah, total, len(schools), schools,
        breakdown_jenjang, breakdown_status,
        active_filters,
        wilayah_level,
        qc_summary=_qc_summary,
        qc_note=_geo_qc_note,
    )
    narasi = await _call_narrator(
                                prompt,
                                fallback="",
                                max_tokens=NARRATOR_TOKENS_LIST
                                )

    if not narasi:
        return template_md

    # Validasi angka — dan-split: cek per-entry, fallback hanya jika >50% missing
    _dan_split_breakdown = active_filters.get("dan_split_breakdown") if active_filters else None
    if _dan_split_breakdown:
        _missing = []
        for _label, _v in _dan_split_breakdown.items():
            if _v and _v >= 10:
                if not _validate_numbers_in_narasi(narasi, {_label: _v}):
                    _missing.append((_label, _v))

        if _missing:
            _total_validated = sum(1 for _v in _dan_split_breakdown.values() if _v and _v >= 10)
            _missing_ratio = len(_missing) / _total_validated if _total_validated > 0 else 0
            logger.warning(
                "[Narrator] Validasi angka dan-split: %d/%d entry missing di narasi — %s",
                len(_missing), _total_validated, _missing,
            )
            if _missing_ratio > 0.5:
                return _enrich_template_on_validation_fail(
                    template_md, _dan_split_breakdown, context="school_list dan-split"
                )
            else:
                _missing_note = ", ".join(
                    f"{_lbl}: {int(_mv):,}".replace(",", ".")
                    for _lbl, _mv in _missing
                )
                return narasi + f"\n\n> _Koreksi data: {_missing_note}._"
    else:
        if not _validate_numbers_in_narasi(narasi, {"total": total}):
            logger.warning(
                "[Narrator] Validasi angka gagal (school_list) — expected total=%s, pakai template",
                total,
            )
            return _enrich_template_on_validation_fail(
                template_md, {"total ditemukan": total}, context="school_list"
            )

    return narasi


async def narrate_school_profile(
    profile_data: Dict[str, Any],
    template_md: str,
    query: str = "",
    is_weak_match: bool = False,
) -> str:
    """Narasi profil sekolah. profile_data adalah dict hasil dari ES."""
    _weak_disclaimer = (
        "\n\n> ⚠️ **Catatan:** Hasil ini merupakan kecocokan terdekat berdasarkan nama "
        "dan lokasi. Jika bukan sekolah yang dimaksud, coba tambahkan kecamatan, "
        "kabupaten, atau gunakan NPSN untuk pencarian yang lebih akurat."
    ) if is_weak_match else ""

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

    prompt = _prompt_school_profile(profile_data, query=query)
    narasi = await _call_narrator(
                                prompt,
                                fallback="",
                                max_tokens=NARRATOR_TOKENS_PROFILE
                                )

    if not narasi:
        return f"{template_md}{_weak_disclaimer}"

    # Validasi angka kritis: guru dan siswa wajib ada di narasi
    f_check = _extract_profile_fields(profile_data)
    expected = {k: v for k, v in {
        "guru_total": f_check.get("guru_total") or None,
        "siswa":      f_check.get("siswa") or None,
    }.items() if v}
    if expected and not _validate_numbers_in_narasi(narasi, expected):
        logger.warning("[Narrator] Validasi angka gagal (profile), pakai template")
        return f"{template_md}{_weak_disclaimer}"

    # Validasi: nama sekolah wajib ada persis di narasi (tanpa diubah LLM)
    nama_sekolah = (profile_data or {}).get("nama", "")
    if nama_sekolah and nama_sekolah.lower() not in narasi.lower():
        logger.warning(
            "[Narrator] Nama sekolah '%s' tidak ditemukan di narasi, pakai template",
            nama_sekolah,
        )
        return f"{template_md}{_weak_disclaimer}"

    return f"{narasi}{_weak_disclaimer}"


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
        max_tokens=1500,
    )

    expected_numbers = _extract_numbers_from_text(merged_context)
    if expected_numbers:
        if not _validate_numbers_in_narasi(narasi, expected_numbers):
            logger.warning("[Narrator] Validasi angka gagal (multi_module), pakai merged_context")
            return merged_context

    return narasi


# ============================================================
# MULTI-METRIC NARRATOR (Phase 21-A)
# ============================================================

async def narrate_multi_metric_aggregation(
    wilayah: str,
    level: str,
    multi_metric: Dict[str, Any],
    active_filters: Optional[Dict] = None,
) -> str:
    """
    Narasi untuk aggregation multi-metric (dua atau lebih metric sekaligus).
    Contoh: siswa laki-laki + siswa perempuan di Jawa Barat.
    """
    if not multi_metric:
        return "Data tidak ditemukan untuk lokasi yang dimaksud."

    # Template fallback: bullet list sederhana
    lines = []
    for data in multi_metric.values():
        total_str = f"{data['total']:,}".replace(",", ".")
        lines.append(f"- **{data['label'].title()}**: {total_str}")
    template_md = f"### Data {wilayah}\n\n" + "\n".join(lines)

    prompt = build_multi_metric_aggregation_prompt(
        wilayah=wilayah,
        level=level,
        multi_metric=multi_metric,
        active_filters=active_filters,
    )

    narasi = await _call_narrator(
        prompt,
        fallback=template_md,
        max_tokens=NARRATOR_TOKENS_AGGREGATION,
        timeout=NARRATOR_TIMEOUT,
    )

    if not narasi:
        return template_md

    # Validasi: semua angka total wajib ada di narasi
    expected = {data["label"]: data["total"] for data in multi_metric.values()}
    if not _validate_numbers_in_narasi(narasi, expected):
        logger.warning(
            "[Narrator] Validasi angka gagal (multi_metric) — expected=%s, pakai template",
            expected,
        )
        return _enrich_template_on_validation_fail(template_md, expected, context="multi_metric")

    return narasi
