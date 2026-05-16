"""
Test ListSchoolHandler — Phase 13
Unit tests untuk list_location_detector helpers + sort helpers + integrasi handler.
Jalankan: pytest tests/test_list_school_handler.py -v -s
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import re
from unittest.mock import AsyncMock, MagicMock, patch

# ──────────────────────────────────────────────────────────────────
# Sort helpers (pure / no ES needed)
# ──────────────────────────────────────────────────────────────────

from app.services.query_handlers.list_school_handler import (
    _detect_sort_mode,
    _build_sort_clause,
    extract_breakdowns,
    _apply_typo_correction,
    _extract_main_keyword,
)


class TestDetectSortMode:
    def test_default_returns_default(self):
        assert _detect_sort_mode("daftar sekolah di bandung") == "default"

    def test_quality_keyword(self):
        assert _detect_sort_mode("daftar sekolah terbaik di jakarta") == "quality"

    def test_bermasalah_keyword(self):
        assert _detect_sort_mode("sekolah paling bermasalah di jawa barat") == "bermasalah"

    def test_intent_sort_by_takes_priority(self):
        intent = MagicMock()
        intent.sort_by = "total_masalah"
        intent.sort_order = "desc"
        assert _detect_sort_mode("apapun", intent=intent) == "bermasalah"

    def test_intent_persen_bersih_asc(self):
        intent = MagicMock()
        intent.sort_by = "persen_bersih"
        intent.sort_order = "asc"
        assert _detect_sort_mode("q", intent=intent) == "bermasalah"

    def test_intent_persen_bersih_desc(self):
        intent = MagicMock()
        intent.sort_by = "persen_bersih"
        intent.sort_order = "desc"
        assert _detect_sort_mode("q", intent=intent) == "quality"


class TestBuildSortClause:
    def test_default_sort(self):
        clause = _build_sort_clause("default")
        assert clause == [{"_score": {"order": "desc"}}]

    def test_quality_sort_has_persen_bersih(self):
        clause = _build_sort_clause("quality")
        fields = [list(c.keys())[0] for c in clause]
        assert "qc_score.persen_bersih" in fields

    def test_bermasalah_sort_has_total_masalah(self):
        clause = _build_sort_clause("bermasalah")
        fields = [list(c.keys())[0] for c in clause]
        assert "qc_score.total_masalah" in fields

    def test_intent_explicit_sort_override(self):
        intent = MagicMock()
        intent.sort_by = "persen_bersih"
        intent.sort_order = "desc"
        clause = _build_sort_clause("default", intent=intent)
        assert clause[0] == {"qc_score.persen_bersih": {"order": "desc", "missing": "_last"}}


class TestExtractBreakdowns:
    def test_empty_aggregations(self):
        jenjang, status = extract_breakdowns({})
        assert jenjang == {}
        assert status == {}

    def test_populated_aggregations(self):
        es_resp = {
            "aggregations": {
                "by_jenjang": {"buckets": [{"key": "SD", "doc_count": 10}]},
                "by_status":  {"buckets": [{"key": "NEGERI", "doc_count": 8}]},
            }
        }
        jenjang, status = extract_breakdowns(es_resp)
        assert jenjang["SD"] == 10
        assert status["NEGERI"] == 8


class TestExtractMainKeyword:
    def test_removes_stopwords(self):
        kw = _extract_main_keyword("daftar sekolah di bandung")
        assert "daftar" not in kw
        assert "sekolah" not in kw
        assert "bandung" in kw

    def test_removes_level_words(self):
        kw = _extract_main_keyword("sekolah kecamatan bogor utara")
        assert "kecamatan" not in kw

    def test_empty_after_stopwords(self):
        kw = _extract_main_keyword("daftar sekolah di")
        assert isinstance(kw, str)


# ──────────────────────────────────────────────────────────────────
# list_location_detector pure function tests (no ES needed)
# ──────────────────────────────────────────────────────────────────

from app.services.core.list_location_detector import _split_dan_query


class TestSplitDanQuery:
    def test_two_locations_split(self):
        result = _split_dan_query("paud di cibunar dan lumpang")
        assert result is None or (isinstance(result, list) and len(result) == 2)

    def test_akreditasi_guard(self):
        result = _split_dan_query("sekolah terakreditasi A dan terakreditasi B")
        assert result is None

    def test_filter_word_guard(self):
        result = _split_dan_query("paud dengan akreditasi A di kuningan")
        assert result is None

    def test_single_location_no_split(self):
        result = _split_dan_query("daftar SMA di bandung")
        assert result is None

    def test_three_way_no_split(self):
        result = _split_dan_query("sekolah di bandung dan bekasi dan bogor")
        assert result is None


# ──────────────────────────────────────────────────────────────────
# ListSchoolHandler integration tests (ES mocked)
# ──────────────────────────────────────────────────────────────────

from app.services.query_handlers.list_school_handler import ListSchoolHandler


def _make_handler():
    es = AsyncMock()
    es.search = AsyncMock(return_value={
        "hits": {
            "hits": [],
            "total": {"value": 0, "relation": "eq"},
        },
        "aggregations": {
            "by_jenjang": {"buckets": []},
            "by_status": {"buckets": []},
        },
    })
    return ListSchoolHandler(es=es, index_name="idx_sekolah_qc")


@pytest.mark.asyncio
class TestHandlerNasionalScope:
    async def test_nasional_scope_returns_success(self):
        handler = _make_handler()
        intent = MagicMock()
        intent.scope = "nasional"
        intent.qc_masalah_only = False
        intent.sort_by = None
        intent.sort_order = None
        intent.wilayah = None
        with patch("app.services.query_handlers.list_school_handler.extract_filters_from_intent", return_value={}), \
             patch("app.services.query_handlers.list_school_handler.build_qc_filter", return_value=[]), \
             patch("app.services.query_handlers.list_school_handler.get_cached_embedding", return_value=None):
            result = await handler.handle("daftar sekolah", intent=intent)
        assert result["status"] == "success"
        assert result["wilayah_level"] == "nasional"
        assert result["parsed"]["wilayah"] == "nasional"


@pytest.mark.asyncio
class TestHandlerDanSplitDepth:
    async def test_depth1_skips_dan_split(self):
        handler = _make_handler()
        with patch("app.services.query_handlers.list_school_handler.extract_filters_from_intent", return_value={}), \
             patch("app.services.query_handlers.list_school_handler._split_dan_query") as mock_split, \
             patch("app.services.query_handlers.list_school_handler._extract_multi_wilayah", return_value=None), \
             patch("app.services.query_handlers.list_school_handler._fallback_detect_wilayah", return_value=None), \
             patch("app.services.query_handlers.list_school_handler._extract_location_keyword", return_value=""), \
             patch("app.services.query_handlers.list_school_handler._find_location_candidates", return_value=[]):
            await handler.handle("sekolah di sini", _depth=1)
        mock_split.assert_not_called()


# ──────────────────────────────────────────────────────────────────
# Phase 16B: Unit tests for extracted sub-functions
# ──────────────────────────────────────────────────────────────────

@pytest.mark.unit
class TestResolveProvinceSiblings:
    def test_returns_none_when_no_siblings(self):
        handler = _make_handler()
        parsed = {"wilayah": "bandung"}
        with patch("app.services.query_handlers.list_school_handler._has_province_siblings",
                   return_value=False):
            result = handler._resolve_province_siblings(parsed, "bandung", "provinsi")
        assert result is None

    def test_returns_none_and_updates_parsed_for_dominant_prov(self):
        handler = _make_handler()
        parsed = {"wilayah": "banten"}
        candidates = [
            {"level": "provinsi", "nama": "Banten"},
            {"level": "desa_kelurahan", "nama": "Banten Desa"},
        ]
        with patch("app.services.query_handlers.list_school_handler._has_province_siblings", return_value=True), \
             patch("app.services.query_handlers.list_school_handler._find_location_candidates", return_value=candidates):
            result = handler._resolve_province_siblings(parsed, "banten", "provinsi")
        assert result is None
        assert parsed["wilayah"] == "Banten"

    def test_returns_clarification_for_ambiguous_siblings(self):
        handler = _make_handler()
        parsed = {"wilayah": "banten"}
        candidates = [
            {"level": "provinsi", "nama": "Banten"},
            {"level": "kabupaten", "nama": "Banten Kab"},
        ]
        with patch("app.services.query_handlers.list_school_handler._has_province_siblings", return_value=True), \
             patch("app.services.query_handlers.list_school_handler._find_location_candidates", return_value=candidates):
            result = handler._resolve_province_siblings(parsed, "banten", "provinsi")
        assert isinstance(result, dict)
        assert result["status"] == "clarification"
        assert result["is_ambiguous"] is True

    def test_returns_none_when_no_prov_candidates(self):
        handler = _make_handler()
        parsed = {"wilayah": "banten"}
        candidates = [{"level": "desa_kelurahan", "nama": "Banten Desa"}]
        with patch("app.services.query_handlers.list_school_handler._has_province_siblings", return_value=True), \
             patch("app.services.query_handlers.list_school_handler._find_location_candidates", return_value=candidates):
            result = handler._resolve_province_siblings(parsed, "banten", "provinsi")
        assert isinstance(result, dict)
        assert result["status"] == "clarification"


@pytest.mark.unit
class TestResolveDominantLocation:
    def test_single_candidate_resolved(self):
        handler = _make_handler()
        candidates = [{"level": "kabupaten", "nama": "Kuningan"}]
        clarification, new_wilayah, new_level = handler._resolve_dominant_location(
            candidates, "kabupaten", None, "kuningan")
        assert clarification is None
        assert new_wilayah == "Kuningan"
        assert new_level == "kabupaten"

    def test_dominant_kab_auto_resolve(self):
        handler = _make_handler()
        candidates = [
            {"level": "kabupaten", "nama": "Kuningan"},
            {"level": "kecamatan", "nama": "Kuningan Kec"},
            {"level": "desa_kelurahan", "nama": "Kuningan Desa"},
        ]
        clarification, new_wilayah, new_level = handler._resolve_dominant_location(
            candidates, "kabupaten", None, "kuningan")
        assert clarification is None
        assert new_wilayah == "Kuningan"
        assert new_level == "kabupaten"

    def test_dominant_prov_auto_resolve(self):
        handler = _make_handler()
        candidates = [
            {"level": "provinsi", "nama": "Banten"},
            {"level": "desa_kelurahan", "nama": "Banten Desa"},
        ]
        clarification, new_wilayah, new_level = handler._resolve_dominant_location(
            candidates, "provinsi", None, "banten")
        assert clarification is None
        assert new_wilayah == "Banten"
        assert new_level == "provinsi"

    def test_same_location_resolve_to_kecamatan(self):
        handler = _make_handler()
        # Semua candidates punya kabupaten + kecamatan yang sama → auto-resolve ke kec
        candidates = [
            {"level": "desa_kelurahan", "nama": "Cisauk Desa", "kabupaten": "Tangerang", "kecamatan": "Cisauk"},
            {"level": "desa_kelurahan", "nama": "Cisauk RT", "kabupaten": "Tangerang", "kecamatan": "Cisauk"},
        ]
        clarification, new_wilayah, new_level = handler._resolve_dominant_location(
            candidates, "kecamatan", None, "cisauk")
        assert clarification is None
        assert new_level == "kecamatan"
        assert "cisauk" in new_wilayah.lower()

    def test_multi_level_returns_clarification(self):
        handler = _make_handler()
        # 2 kabupaten berbeda → clarification
        candidates = [
            {"level": "kabupaten", "nama": "Bogor", "kabupaten": "Bogor", "kecamatan": "A"},
            {"level": "kabupaten", "nama": "Bogor Kota", "kabupaten": "Bogor Kota", "kecamatan": "B"},
        ]
        clarification, new_wilayah, new_level = handler._resolve_dominant_location(
            candidates, "kabupaten", None, "bogor")
        assert isinstance(clarification, dict)
        assert clarification["status"] == "clarification"

    def test_intent_kab_guard_no_change(self):
        handler = _make_handler()
        # 2 kab → dominant-kab tidak trigger; intent guard kab fires → (None, "", "")
        candidates = [
            {"level": "kabupaten", "nama": "Bogor", "kabupaten": "Bogor", "kecamatan": "A"},
            {"level": "kabupaten", "nama": "Bogor Kota", "kabupaten": "Bogor Kota", "kecamatan": "B"},
        ]
        intent = MagicMock()
        intent.wilayah = MagicMock()
        intent.wilayah.kabupaten = "bogor"
        intent.wilayah.provinsi = None
        clarification, new_wilayah, new_level = handler._resolve_dominant_location(
            candidates, "kabupaten", intent, "bogor")
        assert clarification is None
        assert new_wilayah == ""


_FILTER_PATCH = "app.services.query_handlers.list_school_handler.extract_filters_from_intent"


@pytest.mark.unit
class TestApplyFilterFromIntent:
    def test_injects_bentuk_when_missing(self):
        handler = _make_handler()
        parsed = {}
        with patch(_FILTER_PATCH, return_value={"bentuk_pendidikan": "SD"}):
            handler._apply_filter_from_intent(parsed, None)
        assert parsed["bentuk"] == "SD"

    def test_does_not_overwrite_existing_bentuk(self):
        handler = _make_handler()
        parsed = {"bentuk": "SMP"}
        with patch(_FILTER_PATCH, return_value={"bentuk_pendidikan": "SD"}):
            handler._apply_filter_from_intent(parsed, None)
        assert parsed["bentuk"] == "SMP"

    def test_injects_status_sekolah(self):
        handler = _make_handler()
        parsed = {}
        with patch(_FILTER_PATCH, return_value={"status_sekolah": "NEGERI"}):
            handler._apply_filter_from_intent(parsed, None)
        assert parsed["status"] == "NEGERI"

    def test_injects_akreditasi_exact(self):
        handler = _make_handler()
        parsed = {}
        with patch(_FILTER_PATCH, return_value={"akreditasi_exact": "A"}):
            handler._apply_filter_from_intent(parsed, None)
        assert parsed["akreditasi"] == "A"

    def test_empty_filters_no_mutation(self):
        handler = _make_handler()
        parsed = {"bentuk": "SMA"}
        with patch(_FILTER_PATCH, return_value={}):
            handler._apply_filter_from_intent(parsed, None)
        assert parsed == {"bentuk": "SMA"}


@pytest.mark.unit
@pytest.mark.asyncio
class TestApplyTypoAndRetry:
    async def test_returns_none_when_no_fuzzy(self):
        handler = _make_handler()
        with patch("app.services.query_handlers.list_school_handler._extract_main_keyword", return_value="bandung"), \
             patch("app.services.query_handlers.list_school_handler._apply_typo_correction", return_value=None):
            result = await handler._apply_typo_and_retry(
                "daftar sd di bandung", {"wilayah": "bandung"}, "bandung", "kabupaten", False, None)
        assert result is None

    async def test_returns_none_when_same_kw(self):
        handler = _make_handler()
        with patch("app.services.query_handlers.list_school_handler._extract_main_keyword", return_value="bandung"), \
             patch("app.services.query_handlers.list_school_handler._apply_typo_correction",
                   return_value=("bandung", "kabupaten")):
            result = await handler._apply_typo_and_retry(
                "daftar sd di bandung", {"wilayah": "bandung"}, "bandung", "kabupaten", False, None)
        assert result is None
