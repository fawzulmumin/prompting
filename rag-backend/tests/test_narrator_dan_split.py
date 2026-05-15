"""
Tests untuk validasi angka dan-split dan _validate_numbers_in_narasi().
Jalankan: pytest tests/test_narrator_dan_split.py -v -m unit
Tidak butuh ES/Redis/LLM.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.narrator_service import (
    _validate_numbers_in_narasi,
    _enrich_template_on_validation_fail,
)


# ─────────────────────────────────────────────────────────────────────────────
# _validate_numbers_in_narasi — format variations (FIX 2)
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateNumbersFormats:
    def test_exact_match(self):
        assert _validate_numbers_in_narasi("terdapat 150 sekolah", {"total": 150}) is not None

    def test_titik_separator_ribuan(self):
        """LLM menulis 1.234 untuk angka 1234."""
        assert _validate_numbers_in_narasi(
            "Di Jakarta terdapat 1.234 sekolah.", {"jakarta": 1234}
        ) is not None

    def test_koma_separator_ribuan(self):
        """LLM menulis 1,234 untuk angka 1234."""
        assert _validate_numbers_in_narasi(
            "Di Surabaya terdapat 1,234 sekolah.", {"surabaya": 1234}
        ) is not None

    def test_spasi_separator_ribuan(self):
        """LLM menulis 1 234 untuk angka 1234."""
        assert _validate_numbers_in_narasi(
            "Di Bandung terdapat 1 234 sekolah.", {"bandung": 1234}
        ) is not None

    def test_toleransi_2pct_pass(self):
        """Angka dalam toleransi ±2% dianggap valid."""
        # 150 * 0.02 = 3, jadi 148 masih valid
        assert _validate_numbers_in_narasi(
            "Terdapat sekitar 148 sekolah.", {"total": 150}
        ) is not None

    def test_toleransi_2pct_fail(self):
        """Angka di luar toleransi ±2% dianggap gagal."""
        # 150 * 0.02 = 3, jadi 100 jauh di luar range
        assert _validate_numbers_in_narasi(
            "Terdapat sekitar 100 sekolah.", {"total": 150}
        ) is None

    def test_validate_angka_kecil_juga_dicek(self):
        """_validate_numbers_in_narasi() cek SEMUA nilai; threshold >=10 ada di call site."""
        # Angka 5 tidak ada di narasi → return None (fungsi tidak skip sendiri)
        assert _validate_numbers_in_narasi(
            "Tidak ada angka di sini.", {"kecil": 5}
        ) is None

    def test_angka_tidak_ada_di_narasi(self):
        """Narasi tanpa angka → gagal jika expected >= 10."""
        assert _validate_numbers_in_narasi(
            "Terdapat banyak sekolah di kota tersebut.", {"total": 200}
        ) is None

    def test_multiple_entries_all_present(self):
        """Semua entry harus ada di narasi."""
        assert _validate_numbers_in_narasi(
            "Jakarta 150 sekolah, Surabaya 200 sekolah.",
            {"jakarta": 150, "surabaya": 200}
        ) is not None

    def test_multiple_entries_one_missing(self):
        """Satu entry missing → return None."""
        assert _validate_numbers_in_narasi(
            "Jakarta 150 sekolah.",
            {"jakarta": 150, "surabaya": 200}
        ) is None


# ─────────────────────────────────────────────────────────────────────────────
# Logika validasi dan-split di narrate_school_list (FIX 1)
# ─────────────────────────────────────────────────────────────────────────────

class TestDanSplitValidationLogic:
    """Unit test logika per-entry + 50% threshold tanpa memanggil LLM."""

    def _run_validation(self, narasi: str, breakdown: dict):
        """
        Replika logika FIX 1 dari narrator_service.py:588–612.
        Return: (missing_list, missing_ratio, action)
        action = "ok" | "append_koreksi" | "fallback_template"
        """
        missing = []
        for label, v in breakdown.items():
            if v and v >= 10:
                if not _validate_numbers_in_narasi(narasi, {label: v}):
                    missing.append((label, v))

        if not missing:
            return missing, 0.0, "ok"

        total_validated = sum(1 for v in breakdown.values() if v and v >= 10)
        missing_ratio = len(missing) / total_validated if total_validated > 0 else 0

        if missing_ratio > 0.5:
            return missing, missing_ratio, "fallback_template"
        else:
            return missing, missing_ratio, "append_koreksi"

    def test_semua_angka_ada_no_warning(self):
        """Test 1: semua angka ada → action=ok, tidak ada koreksi."""
        breakdown = {"jakarta": 150, "surabaya": 200}
        narasi = "Di Jakarta terdapat 150 sekolah, sedangkan Surabaya memiliki 200 sekolah."
        missing, ratio, action = self._run_validation(narasi, breakdown)
        assert action == "ok"
        assert missing == []

    def test_sebagian_missing_append_koreksi(self):
        """Test 2: 1/3 missing (< 50%) → append koreksi, narasi LLM dipertahankan."""
        breakdown = {"jakarta": 150, "surabaya": 200, "bandung": 75}
        narasi = "Di Jakarta terdapat 150 sekolah dan Surabaya 200 sekolah."  # bandung missing
        missing, ratio, action = self._run_validation(narasi, breakdown)
        assert action == "append_koreksi"
        assert len(missing) == 1
        assert missing[0][0] == "bandung"
        assert ratio < 0.5

    def test_mayoritas_missing_fallback_template(self):
        """Test 3: semua angka missing (100% > 50%) → fallback ke template."""
        breakdown = {"jakarta": 150, "surabaya": 200}
        narasi = "Terdapat banyak sekolah di kedua kota tersebut."  # tidak ada angka
        missing, ratio, action = self._run_validation(narasi, breakdown)
        assert action == "fallback_template"
        assert ratio > 0.5

    def test_tepat_50pct_missing_tidak_fallback(self):
        """50% missing → threshold adalah > 0.5, jadi 50% masih append_koreksi."""
        breakdown = {"jakarta": 150, "surabaya": 200}
        narasi = "Di Jakarta terdapat 150 sekolah."  # surabaya missing (1/2 = 50%)
        missing, ratio, action = self._run_validation(narasi, breakdown)
        assert ratio == 0.5
        assert action == "append_koreksi"  # > 0.5, bukan >= 0.5

    def test_angka_format_titik_tidak_trigger_missing(self):
        """Test 4: angka dengan format 1.234 tetap terdeteksi, tidak trigger missing."""
        breakdown = {"jakarta": 1234}
        narasi = "Di Jakarta terdapat 1.234 sekolah."
        missing, ratio, action = self._run_validation(narasi, breakdown)
        assert action == "ok"
        assert missing == []

    def test_skip_angka_di_bawah_threshold(self):
        """Entry dengan value < 10 di-skip, tidak dihitung ke total_validated."""
        breakdown = {"kecil": 5, "jakarta": 150}
        narasi = "Di Jakarta terdapat 150 sekolah."  # kecil=5 di-skip
        missing, ratio, action = self._run_validation(narasi, breakdown)
        assert action == "ok"

    def test_koreksi_note_format(self):
        """Koreksi append format: '> _Koreksi data: bandung: 75._'"""
        breakdown = {"jakarta": 150, "surabaya": 200, "bandung": 75}
        narasi = "Di Jakarta terdapat 150 sekolah dan Surabaya 200 sekolah."
        missing, ratio, action = self._run_validation(narasi, breakdown)
        # Simulasi append koreksi
        missing_note = ", ".join(
            f"{lbl}: {int(mv):,}".replace(",", ".")
            for lbl, mv in missing
        )
        result = narasi + f"\n\n> _Koreksi data: {missing_note}._"
        assert "Koreksi data" in result
        assert "bandung: 75" in result
        assert narasi[:20] in result  # narasi asli masih ada


# ─────────────────────────────────────────────────────────────────────────────
# _enrich_template_on_validation_fail — format output
# ─────────────────────────────────────────────────────────────────────────────

class TestEnrichTemplateOnFail:
    def test_append_data_ringkas(self):
        result = _enrich_template_on_validation_fail(
            "### Daftar Sekolah\n\n...",
            {"jakarta": 100, "surabaya": 150},
        )
        assert "Data ringkas" in result
        assert "jakarta: 100" in result
        assert "surabaya: 150" in result

    def test_template_tetap_ada(self):
        template = "### Daftar Sekolah\n\n..."
        result = _enrich_template_on_validation_fail(template, {"x": 50})
        assert result.startswith(template)

    def test_empty_expected_returns_template_as_is(self):
        template = "### Daftar"
        assert _enrich_template_on_validation_fail(template, {}) == template

    def test_format_angka_ribuan_indonesia(self):
        """Angka besar diformat dengan titik sebagai separator ribuan."""
        result = _enrich_template_on_validation_fail("T", {"jakarta": 1234})
        assert "1.234" in result


# ─────────────────────────────────────────────────────────────────────────────
# asyncio.gather parallel execution — FIX 3 smoke test
# ─────────────────────────────────────────────────────────────────────────────

class TestDanSplitParallel:
    @pytest.mark.asyncio
    async def test_gather_returns_all_results(self):
        """Pastikan asyncio.gather() mengembalikan hasil untuk semua sub-query."""
        import asyncio

        async def _fake_sub(val):
            return {"status": "success", "total": val, "schools": [], "parsed": {"wilayah": f"kota_{val}"}}

        results = await asyncio.gather(
            _fake_sub(100),
            _fake_sub(200),
            return_exceptions=True,
        )
        assert len(results) == 2
        assert results[0]["total"] == 100
        assert results[1]["total"] == 200

    @pytest.mark.asyncio
    async def test_gather_isolates_exception(self):
        """Exception pada satu sub-query tidak membatalkan yang lain."""
        import asyncio

        async def _ok():
            return {"status": "success", "total": 99, "schools": [], "parsed": {"wilayah": "ok"}}

        async def _fail():
            raise ValueError("ES error simulasi")

        results = await asyncio.gather(_ok(), _fail(), return_exceptions=True)
        assert results[0]["total"] == 99
        assert isinstance(results[1], ValueError)

    @pytest.mark.asyncio
    async def test_gather_concurrent_not_sequential(self):
        """Parallel tasks selesai lebih cepat dari sequential."""
        import asyncio
        import time

        async def _slow():
            await asyncio.sleep(0.05)
            return {"status": "success", "total": 1, "schools": [], "parsed": {"wilayah": "x"}}

        start = time.monotonic()
        await asyncio.gather(_slow(), _slow(), _slow())
        elapsed = time.monotonic() - start
        # 3 tasks parallel ~50ms, sequential ~150ms
        assert elapsed < 0.13, f"Expected parallel <130ms, got {elapsed*1000:.0f}ms"
