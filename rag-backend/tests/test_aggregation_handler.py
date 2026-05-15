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
    _format_ranking_result,
    is_aggregation_query,
    is_ranking_query,
    is_pure_ranking_query,
    detect_ranking_level,
    _extract_ranking_size,
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


# ── _build_resolution: province alias upgrade ─────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_resolution_jakarta_upgrades_to_province(mock_es):
    """intent.wilayah.kabupaten='jakarta' (bare) → entities has provinsi DKI, not kabupaten."""
    intent = make_intent(scope="kabupaten", kabupaten="jakarta")
    result = await _build_resolution("SD di jakarta", mock_es, intent, {})
    assert result["status"] == ResultStatus.SUCCESS
    entities = result["data"]["entities"]
    assert "provinsi" in entities
    assert entities["provinsi"] == "prov. dki jakarta"
    assert "kabupaten" not in entities


@pytest.mark.unit
@pytest.mark.asyncio
async def test_build_resolution_yogyakarta_upgrades_to_province(mock_es):
    """intent.wilayah.kabupaten='yogyakarta' (bare) → entities has provinsi DIY."""
    intent = make_intent(scope="kabupaten", kabupaten="yogyakarta")
    result = await _build_resolution("SD di yogyakarta", mock_es, intent, {})
    assert result["status"] == ResultStatus.SUCCESS
    entities = result["data"]["entities"]
    assert "provinsi" in entities
    assert entities["provinsi"] == "prov. d.i. yogyakarta"
    assert "kabupaten" not in entities


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


# ── Ranking feature tests (Fix 1-4) ──────────────────────────────────────────

@pytest.mark.unit
def test_is_ranking_query_terbanyak_new():
    assert is_ranking_query("5 provinsi dengan SD terbanyak") is True


@pytest.mark.unit
def test_is_ranking_query_peringkat():
    assert is_ranking_query("peringkat kabupaten dengan SMA terbanyak") is True


@pytest.mark.unit
def test_extract_ranking_size_from_n_level():
    assert _extract_ranking_size("5 provinsi dengan SD terbanyak", default=10) == 5


@pytest.mark.unit
def test_extract_ranking_size_from_n_terbanyak():
    assert _extract_ranking_size("10 terbanyak di jawa timur", default=5) == 10


@pytest.mark.unit
def test_extract_ranking_size_default_when_no_n():
    assert _extract_ranking_size("provinsi mana yang paling banyak SMK", default=5) == 5


# ── _format_ranking_result ────────────────────────────────────────────────────

@pytest.mark.unit
def test_format_ranking_result_structure():
    buckets = [
        {"key": "prov. jawa barat", "doc_count": 5000},
        {"key": "prov. jawa timur", "doc_count": 4500},
    ]
    result = _format_ranking_result("5 provinsi SD terbanyak", buckets, jenjang="SD")
    assert result["provider"] == "ELASTICSEARCH_GLOBAL_RANKING"
    assert result["top_location"] == "prov. jawa barat"
    assert result["total"] == 5000
    assert result["ranking"] == buckets
    assert result["jenjang"] == "SD"
    assert result["answer_confident"] is True


@pytest.mark.unit
def test_format_ranking_result_empty_buckets():
    result = _format_ranking_result("query", [])
    assert result["top_location"] is None
    assert result["total"] is None


# ── _handle_ranking_fallback with jenjang_filter ─────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.execute_global_ranking_query")
async def test_ranking_fallback_passes_jenjang_filter(mock_grank, mock_es):
    mock_grank.return_value = [{"key": "prov. jawa barat", "doc_count": 2000}]
    resolution = build_result(status=ResultStatus.SUCCESS)

    await _handle_ranking_fallback(
        query="provinsi dengan SD terbanyak",
        es_client=mock_es,
        resolution=resolution,
        ranking_gap=0.0,
        jenjang_filter="SD",
    )

    call_kwargs = mock_grank.call_args[1]
    assert call_kwargs.get("jenjang_filter") == "SD"


# ── handle_aggregation_query: global ranking for entities={} ─────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.execute_global_ranking_query")
@patch("app.services.query_handlers.aggregation_handler._build_resolution")
async def test_orchestrator_ranking_no_location(mock_resolve, mock_grank, mock_es):
    """'5 provinsi dengan SD terbanyak' → entities={} → global ranking called."""
    mock_resolve.return_value = build_result(
        status=ResultStatus.SUCCESS,
        data={"entities": {}},
        metadata={"national_scope": True},
    )
    mock_grank.return_value = [
        {"key": "prov. jawa timur", "doc_count": 51000},
        {"key": "prov. jawa barat", "doc_count": 48000},
    ]
    intent = make_intent(scope="nasional", jenjang="SD")

    result = await handle_aggregation_query(
        query="5 provinsi dengan SD terbanyak",
        es_client=mock_es,
        intent=intent,
    )

    mock_grank.assert_called_once()
    call_kwargs = mock_grank.call_args[1]
    assert call_kwargs.get("jenjang_filter") == "SD"
    assert call_kwargs.get("size") == 5
    assert result["provider"] == "ELASTICSEARCH_GLOBAL_RANKING"
    assert len(result["ranking"]) == 2


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.execute_global_ranking_query")
@patch("app.services.query_handlers.aggregation_handler._build_resolution")
async def test_orchestrator_filtered_ranking_with_provinsi(mock_resolve, mock_grank, mock_es):
    """'10 kabupaten dengan SMA terbanyak di jawa timur' → filtered ranking within Jatim."""
    mock_resolve.return_value = build_result(
        status=ResultStatus.SUCCESS,
        data={"entities": {"provinsi": "prov. jawa timur"}},
    )
    mock_grank.return_value = [
        {"key": "kota surabaya", "doc_count": 340},
        {"key": "kab. malang",   "doc_count": 280},
    ]
    intent = make_intent(scope="kabupaten", provinsi="prov. jawa timur", jenjang="SMA")

    result = await handle_aggregation_query(
        query="10 kabupaten dengan SMA terbanyak di jawa timur",
        es_client=mock_es,
        intent=intent,
    )

    mock_grank.assert_called_once()
    call_kwargs = mock_grank.call_args[1]
    assert call_kwargs.get("group_field") == "kabupaten"
    assert call_kwargs.get("jenjang_filter") == "SMA"
    assert call_kwargs.get("provinsi_filter") == "prov. jawa timur"
    assert call_kwargs.get("size") == 10
    assert result["provider"] == "ELASTICSEARCH_GLOBAL_RANKING"


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.query_handlers.aggregation_handler.execute_global_ranking_query")
@patch("app.services.query_handlers.aggregation_handler._build_resolution")
async def test_orchestrator_pure_ranking_uses_jenjang(mock_resolve, mock_grank, mock_es):
    """'provinsi mana yang paling banyak SMK?' → pure ranking path, jenjang_filter=SMK."""
    mock_resolve.return_value = build_result(
        status=ResultStatus.SUCCESS,
        data={"entities": {}},
    )
    mock_grank.return_value = [{"key": "prov. jawa timur", "doc_count": 1200}]
    intent = make_intent(scope="nasional", jenjang="SMK")

    result = await handle_aggregation_query(
        query="provinsi mana yang paling banyak SMK?",
        es_client=mock_es,
        intent=intent,
    )

    mock_grank.assert_called_once()
    call_kwargs = mock_grank.call_args[1]
    assert call_kwargs.get("jenjang_filter") == "SMK"
    assert call_kwargs.get("group_field") == "provinsi"
    assert result["provider"] == "ELASTICSEARCH_GLOBAL_RANKING"
