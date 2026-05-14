"""
tests/test_aggregation_handler.py
Unit tests for aggregation_handler.py — no live ES/Redis required.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.query_handlers.aggregation_handler import (
    handle_aggregation_query,
    _handle_nasional_scope,
    _handle_multi_entity,
    _handle_ranking_fallback,
    _build_resolution,
    _apply_entity_bridge,
    _handle_single_wilayah,
    is_aggregation_query,
    is_ranking_query,
    is_pure_ranking_query,
    detect_ranking_level,
)
from app.services.core.result_model import ResultStatus, build_result


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_es():
    es = AsyncMock()
    es.search.return_value = {
        "aggregations": {
            "total": {"value": 1500},
            "by_status": {"buckets": [
                {"key": "NEGERI", "doc_count": 900},
                {"key": "SWASTA", "doc_count": 600},
            ]},
            "by_jenjang": {"buckets": [
                {"key": "SD", "doc_count": 800},
                {"key": "SMP", "doc_count": 700},
            ]},
            "by_provinsi": {"buckets": []},
        }
    }
    return es


def make_intent(scope="nasional", provinsi=None, kabupaten=None,
                jenjang=None, status=None):
    from app.services.core.cognitive_router import CognitiveIntent, WilayahEntities
    wilayah = WilayahEntities(
        provinsi=provinsi,
        kabupaten=kabupaten,
    )
    intent = CognitiveIntent(
        module="aggregation",
        scope=scope,
        secondary=[],
        sub_queries={},
        wilayah=wilayah,
        jenjang=jenjang,
        status=status,
    )
    return intent


# ── Pure helper functions (no mock) ──────────────────────────────────────────

@pytest.mark.unit
def test_is_aggregation_query_berapa():
    assert is_aggregation_query("berapa SD di Bandung") is True


@pytest.mark.unit
def test_is_aggregation_query_jumlah():
    assert is_aggregation_query("jumlah sekolah di Bandung") is True


@pytest.mark.unit
def test_is_aggregation_query_false_for_list():
    assert is_aggregation_query("tampilkan SMA di Bandung") is False


@pytest.mark.unit
def test_is_ranking_query_terbanyak():
    assert is_ranking_query("5 kabupaten dengan SD terbanyak di Jawa Barat") is True


@pytest.mark.unit
def test_is_ranking_query_false():
    assert is_ranking_query("berapa SD di Bandung") is False


@pytest.mark.unit
def test_is_pure_ranking_query():
    # needs: ranking keyword + level word + ("sekolah" OR "mana"/"di indonesia")
    assert is_pure_ranking_query("provinsi dengan sekolah terbanyak") is True


@pytest.mark.unit
def test_is_pure_ranking_query_with_mana():
    assert is_pure_ranking_query("provinsi mana yang paling terbanyak sekolahnya") is True


@pytest.mark.unit
def test_is_pure_ranking_query_false_without_sekolah():
    # Has ranking + level but no sekolah/mana → False
    assert is_pure_ranking_query("provinsi dengan SMK terbanyak") is False


@pytest.mark.unit
def test_is_pure_ranking_query_false_without_ranking():
    assert is_pure_ranking_query("berapa SD di Jawa Barat") is False


@pytest.mark.unit
def test_detect_ranking_level_kabupaten():
    level = detect_ranking_level("10 kabupaten dengan SD terbanyak")
    assert level == "kabupaten"


@pytest.mark.unit
def test_detect_ranking_level_provinsi():
    level = detect_ranking_level("provinsi dengan SMA terbanyak")
    assert level == "provinsi"


# ── _apply_entity_bridge ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_apply_entity_bridge_injects_jenjang():
    intent = make_intent(jenjang="SMA")
    entities = {"provinsi": "prov. jawa barat"}
    data = {}
    result = _apply_entity_bridge(entities, data, intent, {})
    assert result.get("bentuk_pendidikan") == "SMA"


@pytest.mark.unit
def test_apply_entity_bridge_no_double_inject():
    intent = make_intent(jenjang="SMA")
    entities = {"provinsi": "prov. jawa barat", "bentuk_pendidikan": "SD"}
    data = {}
    result = _apply_entity_bridge(entities, data, intent, {})
    # Should NOT overwrite existing bentuk_pendidikan
    assert result["bentuk_pendidikan"] == "SD"


@pytest.mark.unit
def test_apply_entity_bridge_none_entities_passthrough():
    result = _apply_entity_bridge(None, {}, None, {})
    assert result is None


# ── _handle_nasional_scope ────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_nasional_scope_returns_correct_provider(mock_es):
    resolution = build_result(
        status=ResultStatus.SUCCESS,
        metadata={"national_scope": True}
    )
    intent = make_intent(scope="nasional")

    result = await _handle_nasional_scope(
        query="berapa total SD di Indonesia",
        es_client=mock_es,
        intent=intent,
        resolution=resolution,
        ranking_gap=0.0,
    )

    assert result["provider"] == "ELASTICSEARCH_AGGREGATION_NASIONAL"
    assert result["status"] == ResultStatus.SUCCESS
    assert result["total"] == 1500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nasional_scope_breakdown_status(mock_es):
    resolution = build_result(status=ResultStatus.SUCCESS)
    intent = make_intent(scope="nasional")

    result = await _handle_nasional_scope(
        query="berapa sekolah nasional",
        es_client=mock_es,
        intent=intent,
        resolution=resolution,
        ranking_gap=0.0,
    )

    breakdown = result.get("breakdown_status", {})
    assert "NEGERI" in breakdown
    assert breakdown["NEGERI"] == 900


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nasional_scope_entities_empty(mock_es):
    resolution = build_result(status=ResultStatus.SUCCESS)
    intent = make_intent(scope="nasional")

    result = await _handle_nasional_scope(
        query="total sekolah indonesia",
        es_client=mock_es,
        intent=intent,
        resolution=resolution,
        ranking_gap=0.0,
    )

    assert result["entities"] == {}


# ── _handle_multi_entity ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.execute_multi_aggregation_query")
@patch("app.services.query_handlers.aggregation_handler.get_cache", return_value=None)
@patch("app.services.query_handlers.aggregation_handler.set_cache", new_callable=AsyncMock)
async def test_multi_entity_two_provinces(mock_set_cache, mock_get_cache, mock_multi_agg, mock_es):
    mock_multi_agg.return_value = [
        {"entities": {"provinsi": "prov. jawa barat"}, "total": 5000},
        {"entities": {"provinsi": "prov. jawa timur"}, "total": 4500},
    ]
    resolution = build_result(status=ResultStatus.SUCCESS)

    result = await _handle_multi_entity(
        query="berapa SD di Jawa Barat dan Jawa Timur",
        es_client=mock_es,
        entities=[
            {"provinsi": "prov. jawa barat"},
            {"provinsi": "prov. jawa timur"},
        ],
        resolution=resolution,
        ranking_gap=0.0,
    )

    assert result["provider"] == "ELASTICSEARCH_MULTI_AGGREGATION"
    assert result["is_multi"] is True
    assert len(result["comparison"]) == 2


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.get_cache")
async def test_multi_entity_cache_hit(mock_get_cache, mock_es):
    cached_result = {"provider": "ELASTICSEARCH_MULTI_AGGREGATION", "is_multi": True}
    mock_get_cache.return_value = cached_result
    resolution = build_result(status=ResultStatus.SUCCESS)

    result = await _handle_multi_entity(
        query="berapa SD di X dan Y",
        es_client=mock_es,
        entities=[{"provinsi": "a"}, {"provinsi": "b"}],
        resolution=resolution,
        ranking_gap=0.0,
    )
    assert result == cached_result


# ── _handle_ranking_fallback ──────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.execute_global_ranking_query")
async def test_ranking_fallback_returns_none_for_non_ranking(mock_grank, mock_es):
    resolution = build_result(status=ResultStatus.SUCCESS)
    result = await _handle_ranking_fallback(
        query="berapa SD di Bandung",
        es_client=mock_es,
        resolution=resolution,
        ranking_gap=0.0,
    )
    assert result is None
    mock_grank.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.execute_global_ranking_query")
async def test_ranking_fallback_returns_result_for_ranking(mock_grank, mock_es):
    mock_grank.return_value = [
        {"key": "prov. jawa barat", "doc_count": 50000},
        {"key": "prov. jawa timur", "doc_count": 45000},
    ]
    resolution = build_result(status=ResultStatus.SUCCESS)

    result = await _handle_ranking_fallback(
        query="provinsi dengan SD terbanyak",
        es_client=mock_es,
        resolution=resolution,
        ranking_gap=0.0,
    )

    assert result is not None
    assert result["provider"] == "ELASTICSEARCH_GLOBAL_RANKING"
    assert result["top_location"] == "prov. jawa barat"
    assert result["total"] == 50000


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.execute_global_ranking_query")
async def test_ranking_fallback_returns_none_when_empty_buckets(mock_grank, mock_es):
    mock_grank.return_value = []
    resolution = build_result(status=ResultStatus.SUCCESS)

    result = await _handle_ranking_fallback(
        query="provinsi dengan SD terbanyak",
        es_client=mock_es,
        resolution=resolution,
        ranking_gap=0.0,
    )
    assert result is None


# ── handle_aggregation_query (orchestrator level) ─────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler._handle_nasional_scope")
@patch("app.services.query_handlers.aggregation_handler._build_resolution")
async def test_orchestrator_routes_to_nasional(mock_resolve, mock_nasional, mock_es):
    mock_resolve.return_value = build_result(
        status=ResultStatus.SUCCESS,
        data={"entities": {}},
        metadata={"national_scope": True},
    )
    mock_nasional.return_value = {
        "status": ResultStatus.SUCCESS,
        "provider": "ELASTICSEARCH_AGGREGATION_NASIONAL",
        "total": 560000,
        "answer_confident": True,
    }
    intent = make_intent(scope="nasional")

    result = await handle_aggregation_query(
        query="berapa total sekolah Indonesia",
        es_client=mock_es,
        intent=intent,
    )

    mock_nasional.assert_called_once()
    assert result["provider"] == "ELASTICSEARCH_AGGREGATION_NASIONAL"


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler._build_resolution")
async def test_orchestrator_ambiguous_returns_ambiguous_status(mock_resolve, mock_es):
    mock_resolve.return_value = build_result(
        status=ResultStatus.AMBIGUOUS,
        data={"candidates": [
            {"label": "Kab. Bandung"},
            {"label": "Kota Bandung"},
        ]},
    )
    intent = make_intent(scope="kabupaten", kabupaten="bandung")

    result = await handle_aggregation_query(
        query="berapa SD di bandung",
        es_client=mock_es,
        intent=intent,
    )

    assert result["status"] == ResultStatus.AMBIGUOUS
    assert result["provider"] == "SYSTEM_AMBIGUOUS"


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler._handle_single_wilayah")
@patch("app.services.query_handlers.aggregation_handler._build_resolution")
async def test_orchestrator_routes_to_single_wilayah(mock_resolve, mock_single, mock_es):
    mock_resolve.return_value = build_result(
        status=ResultStatus.SUCCESS,
        data={"entities": {"kabupaten": "kab. bandung"}},
    )
    mock_single.return_value = {
        "status": ResultStatus.SUCCESS,
        "provider": "ELASTICSEARCH_AGGREGATION",
        "total": 500,
        "answer_confident": True,
    }
    intent = make_intent(scope="kabupaten", kabupaten="kab. bandung")

    result = await handle_aggregation_query(
        query="berapa SD di Bandung",
        es_client=mock_es,
        intent=intent,
    )

    mock_single.assert_called_once()
    assert result["provider"] == "ELASTICSEARCH_AGGREGATION"
