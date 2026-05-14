"""
Test LocalNarrator fast-path functions (Task 5.2-5.3).
Jalankan: pytest tests/test_local_narrator.py -v
Tidak butuh ES/Redis/LLM — semua sync, template-based.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app.services.core.local_narrator import (
    narrate_qc_pct,
    narrate_aggregation_simple,
    narrate_single_section,
    _fmt,
)


# ─────────────────────────────────────────────────────────────────────────────
# _fmt — Indonesian number format
# ─────────────────────────────────────────────────────────────────────────────

class TestFmt:
    def test_small_number(self):
        assert _fmt(5) == "5"

    def test_thousands(self):
        assert _fmt(1234) == "1.234"

    def test_millions(self):
        assert _fmt(1234567) == "1.234.567"

    def test_zero(self):
        assert _fmt(0) == "0"


# ─────────────────────────────────────────────────────────────────────────────
# narrate_qc_pct
# ─────────────────────────────────────────────────────────────────────────────

class TestNarrateQcPct:
    def test_contains_total_sekolah(self):
        result = narrate_qc_pct(10000, 8000, 2000, 80.0, 20.0)
        assert "10.000" in result

    def test_contains_total_bersih(self):
        result = narrate_qc_pct(10000, 8000, 2000, 80.0, 20.0)
        assert "8.000" in result

    def test_contains_total_bermasalah(self):
        result = narrate_qc_pct(10000, 8000, 2000, 80.0, 20.0)
        assert "2.000" in result

    def test_persen_format_comma(self):
        # persen harus pakai koma desimal, bukan titik
        result = narrate_qc_pct(1000, 800, 200, 80.0, 20.0)
        assert "80,0%" in result
        assert "20,0%" in result

    def test_custom_wilayah_label(self):
        result = narrate_qc_pct(500, 400, 100, 80.0, 20.0, wilayah_label="Kota Bogor")
        assert "Kota Bogor" in result

    def test_default_wilayah_label(self):
        result = narrate_qc_pct(500, 400, 100, 80.0, 20.0)
        assert "seluruh Indonesia" in result

    def test_returns_string(self):
        result = narrate_qc_pct(100, 90, 10, 90.0, 10.0)
        assert isinstance(result, str)
        assert len(result) > 20


# ─────────────────────────────────────────────────────────────────────────────
# narrate_aggregation_simple
# ─────────────────────────────────────────────────────────────────────────────

class TestNarrateAggregationSimple:
    def test_returns_string_for_basic_case(self):
        result = narrate_aggregation_simple("Kota Bogor", 100, "kabupaten")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_formatted_total(self):
        result = narrate_aggregation_simple("Bandung", 1234, "kabupaten")
        assert "1.234" in result

    def test_contains_wilayah_name(self):
        result = narrate_aggregation_simple("serpong", 50, "kecamatan")
        assert "Serpong" in result

    def test_contains_level_label(self):
        result = narrate_aggregation_simple("Jawa Barat", 5000, "provinsi")
        assert "provinsi" in result

    def test_benchmark_sedikit(self):
        # desa level, total=3 → sedikit
        result = narrate_aggregation_simple("Desa X", 3, "desa_kelurahan")
        assert "sedikit" in result

    def test_benchmark_cukup(self):
        # kecamatan level, total=50 → cukup memadai
        result = narrate_aggregation_simple("Ciputat", 50, "kecamatan")
        assert "cukup" in result

    def test_benchmark_banyak(self):
        # kabupaten level, total=3000 → banyak
        result = narrate_aggregation_simple("Garut", 3000, "kabupaten")
        assert "banyak" in result

    def test_returns_none_for_metric_siswa(self):
        # metric non-sekolah harus return None (caller fallback ke LLM)
        result = narrate_aggregation_simple("Bogor", 5000, "kabupaten", metric_target="siswa")
        assert result is None

    def test_returns_none_for_metric_guru(self):
        result = narrate_aggregation_simple("Bogor", 5000, "kabupaten", metric_target="guru")
        assert result is None

    def test_with_jenjang_filter(self):
        result = narrate_aggregation_simple("Tangsel", 200, "kabupaten", jenjang="SMP")
        assert "SMP" in result

    def test_with_status_filter(self):
        result = narrate_aggregation_simple("Surabaya", 150, "kabupaten", status="NEGERI")
        assert "NEGERI" in result

    def test_nasional_level(self):
        result = narrate_aggregation_simple("Indonesia", 560000, "nasional")
        assert "560.000" in result
        assert "nasional" in result

    def test_unknown_level_falls_back_gracefully(self):
        result = narrate_aggregation_simple("Somewhere", 10, "unknown_level")
        assert isinstance(result, str)


# ─────────────────────────────────────────────────────────────────────────────
# narrate_single_section
# ─────────────────────────────────────────────────────────────────────────────

class TestNarrateSingleSection:
    def test_passthrough_plain_text(self):
        ctx = "Terdapat 100 sekolah di wilayah ini."
        assert narrate_single_section(ctx) == ctx

    def test_passthrough_markdown(self):
        ctx = "## Ringkasan\nTerdapat 100 sekolah."
        assert narrate_single_section(ctx) == ctx

    def test_passthrough_empty(self):
        assert narrate_single_section("") == ""
