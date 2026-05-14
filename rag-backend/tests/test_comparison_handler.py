"""
tests/test_comparison_handler.py
Unit tests for comparison_handler.py — no live ES/Redis required.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.query_handlers.comparison_handler import (
    handle_comparison_query,
    _handle_status_comparison,
    _handle_jenjang_comparison,
    _fetch_location_data,
    _build_comparison_markdown,
    _canonicalize_provinsi,
    is_comparison_query,
    detect_status_sekolah,
    _clean_location_part,
    get_display_value,
    graceful_error_response,
)
from app.services.core.result_model import ResultStatus


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_es():
    es = AsyncMock()
    es.search.return_value = {
        "aggregations": {"total_sekolah": {"value": 100}}
    }
    return es


# ── Pure-function helpers (no mocks needed) ───────────────────────────────────

@pytest.mark.unit
def test_canonicalize_provinsi_without_prefix():
    assert _canonicalize_provinsi("jawa barat") == "prov. jawa barat"


@pytest.mark.unit
def test_canonicalize_provinsi_with_prefix_unchanged():
    # must not produce "prov. prov. jawa barat"
    result = _canonicalize_provinsi("prov. jawa barat")
    assert result == "prov. jawa barat"
    assert result.count("prov.") == 1


@pytest.mark.unit
def test_detect_status_negeri():
    assert detect_status_sekolah("sekolah negeri di bandung") == "Negeri"


@pytest.mark.unit
def test_detect_status_swasta():
    assert detect_status_sekolah("sekolah swasta di bogor") == "Swasta"


@pytest.mark.unit
def test_detect_status_none_for_plain_query():
    assert detect_status_sekolah("bandung vs bogor") is None


@pytest.mark.unit
def test_is_comparison_query_vs():
    assert is_comparison_query("bandung vs bogor") is True


@pytest.mark.unit
def test_is_comparison_query_bandingkan():
    assert is_comparison_query("bandingkan sma di bandung dan bogor") is True


@pytest.mark.unit
def test_is_comparison_query_false_for_list():
    # single aggregation query — NOT comparison
    assert is_comparison_query("daftar sekolah di bandung") is False


@pytest.mark.unit
def test_build_comparison_markdown_basic():
    results = [
        {"value": "Bandung", "count": 500, "entities": {}},
        {"value": "Bogor",   "count": 300, "entities": {}},
    ]
    md = _build_comparison_markdown(results)
    assert "Bandung" in md
    assert "500" in md
    assert "Bogor" in md
    assert "terbanyak" in md.lower()


@pytest.mark.unit
def test_build_comparison_markdown_with_jenjang():
    results = [{"value": "Surabaya", "count": 200, "entities": {}}]
    md = _build_comparison_markdown(results, jenjang_label="SMK")
    assert "SMK" in md
    assert "Surabaya" in md


@pytest.mark.unit
def test_get_display_value_kabupaten():
    val = get_display_value({"kabupaten": "kab. bandung"})
    assert "bandung" in val.lower()


@pytest.mark.unit
def test_graceful_error_response_structure():
    r = graceful_error_response()
    assert r["provider"] == "SYSTEM_ERROR"
    assert r["answer_confident"] is False
    assert "answer_markdown" in r


@pytest.mark.unit
def test_clean_location_part_removes_noise():
    result = _clean_location_part("jumlah sekolah di bandung")
    assert "jumlah" not in result.lower()
    assert "sekolah" not in result.lower()
    assert "bandung" in result.lower()


# ── _handle_status_comparison ─────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.comparison_handler.execute_aggregation_query")
@patch("app.services.query_handlers.comparison_handler.validate_hierarchy_integrity")
async def test_status_comparison_negeri_vs_swasta(mock_validate, mock_agg, mock_es):
    mock_validate.return_value = {"status": ResultStatus.SUCCESS}
    mock_agg.side_effect = [150, 80]  # NEGERI=150, SWASTA=80

    result = await _handle_status_comparison(
        query="bandingkan negeri vs swasta di bandung",
        es_client=mock_es,
        entities={"kabupaten": "kab. bandung"},
        statuses=["NEGERI", "SWASTA"],
    )

    assert result["provider"] == "ELASTICSEARCH_COMPARISON_STATUS"
    assert result["num_docs"] == 230
    assert result["answer_confident"] is True
    assert result["results"][0][0] == "NEGERI"
    assert result["results"][0][1] == 150


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.comparison_handler.execute_aggregation_query")
@patch("app.services.query_handlers.comparison_handler.validate_hierarchy_integrity")
async def test_status_comparison_equal(mock_validate, mock_agg, mock_es):
    mock_validate.return_value = {"status": ResultStatus.SUCCESS}
    mock_agg.side_effect = [100, 100]

    result = await _handle_status_comparison(
        query="negeri vs swasta di surabaya",
        es_client=mock_es,
        entities={"kabupaten": "kota surabaya"},
        statuses=["NEGERI", "SWASTA"],
    )

    assert result["provider"] == "ELASTICSEARCH_COMPARISON_STATUS"
    assert "sama" in result["answer_markdown"].lower()


# ── _handle_jenjang_comparison ────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.comparison_handler.execute_aggregation_query")
async def test_jenjang_comparison_sma_vs_smk(mock_agg, mock_es):
    mock_agg.side_effect = [200, 150]  # SMA=200, SMK=150

    result = await _handle_jenjang_comparison(
        query="sma vs smk di bandung",
        es_client=mock_es,
        entities={"kabupaten": "kab. bandung"},
        jenjang_list=["SMA", "SMK"],
    )

    assert result["provider"] == "ELASTICSEARCH_COMPARISON_JENJANG"
    assert result["results"][0][0] == "SMA"
    assert result["results"][0][1] == 200
    assert "SMA" in result["answer_markdown"]
    assert "SMK" in result["answer_markdown"]


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.comparison_handler.execute_aggregation_query")
async def test_jenjang_comparison_total_hits(mock_agg, mock_es):
    mock_agg.side_effect = [50, 75, 30]

    result = await _handle_jenjang_comparison(
        query="sd vs smp vs sma di bogor",
        es_client=mock_es,
        entities={"kabupaten": "kota bogor"},
        jenjang_list=["SD", "SMP", "SMA"],
    )

    assert result["num_docs"] == 155
    assert result["results"][0][0] == "SMP"  # terbanyak


# ── _fetch_location_data ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_fetch_location_data_returns_count(mock_es):
    mock_es.search.return_value = {
        "aggregations": {"total_sekolah": {"value": 42}}
    }
    result = await _fetch_location_data(
        {"kabupaten": "kab. bandung"},
        mock_es,
    )
    assert result["count"] == 42
    assert "value" in result
    assert "entities" in result


# ── handle_comparison_query (integration-level with mocks) ───────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.comparison_handler._resolve_vs_query")
@patch("app.services.query_handlers.comparison_handler._fetch_location_data")
@patch("app.services.query_handlers.comparison_handler.validate_hierarchy_integrity")
async def test_handle_comparison_two_locations(mock_validate, mock_fetch, mock_resolve, mock_es):
    mock_validate.return_value = {"status": ResultStatus.SUCCESS}
    mock_resolve.return_value = [
        {"kabupaten": "kab. bandung"},
        {"kabupaten": "kota bogor"},
    ]
    mock_fetch.side_effect = [
        {"value": "Bandung", "count": 500, "entities": {"kabupaten": "kab. bandung"}},
        {"value": "Bogor",   "count": 300, "entities": {"kabupaten": "kota bogor"}},
    ]

    result = await handle_comparison_query(
        query="bandung vs bogor",
        es_client=mock_es,
    )

    assert result["provider"] == "ELASTICSEARCH_COMPARISON"
    assert result["num_docs"] == 800
    assert result["results"][0][0] == "Bandung"
    assert result["answer_confident"] is True


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.comparison_handler._resolve_vs_query")
async def test_handle_comparison_ambiguous(mock_resolve, mock_es):
    mock_resolve.return_value = {
        "ambiguous": True,
        "ambiguous_part": "cibunar",
        "candidates": [
            {"label": "Desa Cibunar, Kec. Parungpanjang, Kab. Bogor"},
            {"label": "Desa Cibunar, Kec. Cibatu, Kab. Garut"},
        ],
    }

    result = await handle_comparison_query(
        query="cibunar vs sukajaya",
        es_client=mock_es,
    )

    assert result["status"] == ResultStatus.AMBIGUOUS
    assert result["answer_confident"] is False


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.comparison_handler._resolve_vs_query")
async def test_handle_comparison_single_location_returns_validation(mock_resolve, mock_es):
    # Only 1 location resolved — needs 2 for comparison
    mock_resolve.return_value = [{"kabupaten": "kab. bandung"}]

    result = await handle_comparison_query(
        query="bandung",
        es_client=mock_es,
    )

    assert result is not None
    assert result.get("reason") == "COMPARISON_NEEDS_MULTIPLE_LOCATIONS"
    assert result["answer_confident"] is False
