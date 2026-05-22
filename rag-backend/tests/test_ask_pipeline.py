"""
Test ask_pipeline sub-functions — Phase 14
Unit tests untuk sub-fungsi yang diekstrak dari run_ask_pipeline().
Tidak butuh ES/LLM live.
Jalankan: pytest tests/test_ask_pipeline.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.core.ask_pipeline import (
    _resolve_geo_params,
    _extract_radius_from_query,
    _extract_ambiguous_message,
    build_cache_key,
)


# ──────────────────────────────────────────────────────────────────────────────
# _resolve_geo_params (pure / sync)
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveGeoParams:
    def test_explicit_coords_passthrough(self):
        lat, lon, radius = _resolve_geo_params("sekolah dekat sini", 1.0, 2.0, 2.0)
        assert lat == 1.0 and lon == 2.0

    def test_extract_coords_from_query(self):
        lat, lon, radius = _resolve_geo_params(
            "sekolah dekat -6.2088,106.8456", None, None, 2.0)
        assert lat == pytest.approx(-6.2088, rel=1e-3)
        assert lon == pytest.approx(106.8456, rel=1e-3)

    def test_no_coords_returns_none(self):
        lat, lon, radius = _resolve_geo_params("SD di bandung", None, None, 2.0)
        assert lat is None and lon is None

    def test_radius_extracted_from_query(self):
        _, _, radius = _resolve_geo_params("sekolah radius 5km", None, None, 2.0)
        assert radius == 5.0

    def test_radius_default_preserved_when_not_default(self):
        # Jika radius_km bukan 2.0, query radius tidak di-override
        _, _, radius = _resolve_geo_params("sekolah radius 5km", None, None, 3.0)
        assert radius == 3.0

    def test_slash_separator_coords(self):
        lat, lon, _ = _resolve_geo_params("posisi -6.5/107.1", None, None, 2.0)
        assert lat == pytest.approx(-6.5, rel=1e-3)
        assert lon == pytest.approx(107.1, rel=1e-3)


# ──────────────────────────────────────────────────────────────────────────────
# _extract_radius_from_query (pure / sync)
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractRadius:
    def test_km_suffix(self):
        assert _extract_radius_from_query("sekolah dalam radius 5km") == 5.0

    def test_kilometer_suffix(self):
        assert _extract_radius_from_query("radius 3 kilometer") == 3.0

    def test_meter_suffix_converts(self):
        r = _extract_radius_from_query("dalam 500 meter")
        assert r == pytest.approx(0.5, rel=1e-3)

    def test_no_radius_returns_none(self):
        assert _extract_radius_from_query("SD di bandung") is None

    def test_out_of_range_km_ignored(self):
        assert _extract_radius_from_query("radius 200km") is None


# ──────────────────────────────────────────────────────────────────────────────
# build_cache_key (pure / sync)
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildCacheKey:
    def test_deterministic(self):
        key1 = build_cache_key("berapa SD di bandung", "kabupaten")
        key2 = build_cache_key("berapa SD di bandung", "kabupaten")
        assert key1 == key2

    def test_different_queries_different_keys(self):
        key1 = build_cache_key("SD di bandung", None)
        key2 = build_cache_key("SD di bogor", None)
        assert key1 != key2

    def test_key_has_prefix(self):
        key = build_cache_key("apapun", None)
        assert key.startswith("response:")


# ──────────────────────────────────────────────────────────────────────────────
# _extract_ambiguous_message (pure / sync)
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractAmbiguousMessage:
    def test_ambiguous_status_string(self):
        result = {"status": "AMBIGUOUS", "data": {"message": "Apakah maksud Anda: X atau Y?"}}
        msg = _extract_ambiguous_message(result)
        assert msg is not None
        assert "X" in msg

    def test_non_ambiguous_returns_none(self):
        result = {"status": "success", "data": {}}
        assert _extract_ambiguous_message(result) is None

    def test_none_input_returns_none(self):
        assert _extract_ambiguous_message(None) is None

    def test_ambiguous_without_message_uses_candidates(self):
        result = {
            "status": "AMBIGUOUS",
            "data": {"candidates": [{"nama": "SDN 1 Bogor"}, {"nama": "SDN 1 Bogor Barat"}]},
        }
        msg = _extract_ambiguous_message(result)
        # build_ambiguous_message should produce something non-None or empty string
        assert msg is not None


# ──────────────────────────────────────────────────────────────────────────────
# _run_classification (async, mocked classify_intent)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRunClassification:
    @patch("app.services.core.ask_pipeline.classify_intent")
    async def test_returns_intent_tuple(self, mock_classify):
        from app.services.core.cognitive_router import CognitiveIntent
        mock_classify.return_value = CognitiveIntent(module="aggregation")
        from app.services.core.ask_pipeline import _run_classification
        intent, _ = await _run_classification("berapa SD di bandung", on_step=None)
        assert intent.module == "aggregation"

    @patch("app.services.core.ask_pipeline.classify_intent")
    async def test_adjudication_oot_trusted_over_school(self, mock_classify):
        from app.services.core.cognitive_router import CognitiveIntent
        from app.services.core.intent_classifier import IntentType
        # LLM says OOT — adjudication is now skipped, OOT is trusted
        mock_classify.return_value = CognitiveIntent(module="out_of_context")
        with patch("app.services.core.ask_pipeline.rule_classify", return_value=IntentType.SCHOOL):
            from app.services.core.ask_pipeline import _run_classification
            intent, _ = await _run_classification("patokannya sekolah bagus itu apa", on_step=None)
        assert intent.primary == "out_of_context"
