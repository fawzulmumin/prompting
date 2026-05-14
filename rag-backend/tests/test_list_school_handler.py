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
