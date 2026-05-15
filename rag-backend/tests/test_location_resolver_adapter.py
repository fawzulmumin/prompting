"""
tests/test_location_resolver_adapter.py
Unit tests for location_resolver_adapter.resolve_location_from_query().
All tests are pure unit (no ES, no Redis).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.core.result_model import ResultStatus


# ── Fixture helpers ──────────────────────────────────────────────────────────

def _make_resolved(
    provinsi=None, kabupaten=None, kecamatan=None, desa=None,
    confidence=1.0, method="exact", ambiguous=False, candidates=None
):
    from app.services.core.location_resolver import ResolvedWilayah
    return ResolvedWilayah(
        provinsi=provinsi, kabupaten=kabupaten,
        kecamatan=kecamatan, desa=desa,
        confidence=confidence, method=method,
        ambiguous=ambiguous, candidates=candidates or [],
    )


def _mock_resolver(resolved):
    m = MagicMock()
    m.resolve.return_value = resolved
    return m


@pytest.fixture
def mock_es():
    return AsyncMock()


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_single_location_success(mock_get_resolver, mock_es):
    """Simple single-location query → SUCCESS with entities dict."""
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(kabupaten="kab. bandung", provinsi="prov. jawa barat")
    )

    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("jumlah sekolah di bandung", mock_es)

    assert result["status"] == ResultStatus.SUCCESS
    assert result["provider"] == "LOCATION_RESOLVER_ADAPTER"
    entities = result["data"]["entities"]
    assert isinstance(entities, dict)
    assert entities.get("kabupaten") == "kab. bandung"
    assert entities.get("provinsi") == "prov. jawa barat"


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_ambiguous_location(mock_get_resolver, mock_es):
    """Ambiguous location → AMBIGUOUS with candidates list."""
    candidates = [
        {"label": "Desa Cibunar, Kec. Parungpanjang, Kab. Bogor"},
        {"label": "Desa Cibunar, Kec. Cibatu, Kab. Garut"},
    ]
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(desa="cibunar", confidence=0.5, ambiguous=True, candidates=candidates)
    )

    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah di cibunar", mock_es)

    assert result["status"] == ResultStatus.AMBIGUOUS
    assert result["provider"] == "LOCATION_RESOLVER_ADAPTER"
    assert len(result["data"]["candidates"]) == 2


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_not_found_returns_not_found_status(mock_get_resolver, mock_es):
    """Unknown location → NOT_FOUND."""
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(kabupaten="xyzabc", confidence=0.0, method="unresolved")
    )

    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah di xyzabc", mock_es)

    assert result["status"] == ResultStatus.NOT_FOUND
    assert result["confidence"] == 0.0


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_return_structure_matches_resolve_entities_contract(mock_get_resolver, mock_es):
    """Return dict must have exact same keys as resolve_entities() / build_result()."""
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(kabupaten="kab. bogor", provinsi="prov. jawa barat")
    )

    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah di bogor", mock_es)

    required_keys = {"status", "provider", "data", "reason", "metadata", "confidence"}
    assert required_keys.issubset(result.keys()), f"Missing keys: {required_keys - result.keys()}"
    assert isinstance(result["data"], dict)
    assert isinstance(result["metadata"], dict)


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_vs_query_returns_multi_entities(mock_get_resolver, mock_es):
    """VS query → SUCCESS with is_multi=True and list of entity dicts."""
    # resolver called twice (once per part)
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(kabupaten="kab. bandung", provinsi="prov. jawa barat")
    )

    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("bandung vs bogor", mock_es)

    assert result["status"] == ResultStatus.SUCCESS
    assert result["data"].get("is_multi") is True
    entities = result["data"]["entities"]
    assert isinstance(entities, list)
    assert len(entities) >= 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_query_after_noise_strip_returns_not_found(mock_es):
    """Query that reduces to empty after noise removal → NOT_FOUND."""
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("jumlah sekolah sd smp sma", mock_es)

    assert result["status"] == ResultStatus.NOT_FOUND


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_status_sekolah_negeri_in_metadata(mock_get_resolver, mock_es):
    """'negeri' keyword → status_sekolah=NEGERI in metadata."""
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(kabupaten="kab. bandung", provinsi="prov. jawa barat")
    )

    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah negeri di bandung", mock_es)

    assert result["status"] == ResultStatus.SUCCESS
    assert result["metadata"].get("status_sekolah") == "NEGERI"


# ── Province alias tests ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_province_alias_jakarta(mock_es):
    """'jakarta' → provinsi='prov. dki jakarta', kabupaten not set."""
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("jumlah SD di jakarta", mock_es)
    assert result["status"] == ResultStatus.SUCCESS
    assert result["metadata"]["method"] == "province_alias"
    entities = result["data"]["entities"]
    assert entities.get("provinsi") == "prov. dki jakarta"
    assert "kabupaten" not in entities


@pytest.mark.unit
@pytest.mark.asyncio
async def test_province_alias_yogyakarta(mock_es):
    """'yogyakarta' → provinsi='prov. d.i. yogyakarta', kabupaten not set."""
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah di yogyakarta", mock_es)
    assert result["status"] == ResultStatus.SUCCESS
    entities = result["data"]["entities"]
    assert entities.get("provinsi") == "prov. d.i. yogyakarta"
    assert "kabupaten" not in entities


@pytest.mark.unit
@pytest.mark.asyncio
async def test_province_alias_jogja(mock_es):
    """'jogja' shorthand → provinsi='prov. d.i. yogyakarta'."""
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah di jogja", mock_es)
    assert result["status"] == ResultStatus.SUCCESS
    assert result["data"]["entities"].get("provinsi") == "prov. d.i. yogyakarta"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_province_alias_jatim(mock_es):
    """'jatim' shorthand → provinsi='prov. jawa timur'."""
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("SD di jatim", mock_es)
    assert result["status"] == ResultStatus.SUCCESS
    assert result["data"]["entities"].get("provinsi") == "prov. jawa timur"


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_explicit_kota_prefix_skips_province_alias(mock_get_resolver, mock_es):
    """'kota yogyakarta' has explicit 'kota' prefix → NOT a province alias, resolver handles it."""
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(kabupaten="kota yogyakarta", provinsi="prov. d.i. yogyakarta")
    )
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah di kota yogyakarta", mock_es)
    assert result["status"] == ResultStatus.SUCCESS
    entities = result["data"]["entities"]
    assert entities.get("kabupaten") == "kota yogyakarta"


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_jakarta_selatan_not_province_alias(mock_get_resolver, mock_es):
    """'jakarta selatan' is a specific borough, not in PROVINCE_ALIAS_MAP → LocationResolver."""
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(kabupaten="kota adm. jakarta selatan")
    )
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah di jakarta selatan", mock_es)
    assert result["status"] == ResultStatus.SUCCESS
    entities = result["data"]["entities"]
    assert entities.get("kabupaten") == "kota adm. jakarta selatan"
    assert entities.get("provinsi") is None


@pytest.mark.unit
@pytest.mark.asyncio
@patch("app.services.core.location_resolver_adapter._get_resolver")
async def test_kab_aceh_explicit_prefix_skips_alias(mock_get_resolver, mock_es):
    """'kab. aceh besar' has 'kab.' prefix → explicit kabupaten, NOT a province alias."""
    mock_get_resolver.return_value = _mock_resolver(
        _make_resolved(kabupaten="kab. aceh besar")
    )
    from app.services.core.location_resolver_adapter import resolve_location_from_query
    result = await resolve_location_from_query("sekolah di kab. aceh besar", mock_es)
    assert result["status"] == ResultStatus.SUCCESS
    assert result["data"]["entities"].get("kabupaten") == "kab. aceh besar"


# ── Regression: handler imports ───────────────────────────────────────────────

@pytest.mark.unit
def test_aggregation_handler_still_imports():
    """aggregation_handler imports without error after switching to adapter."""
    import importlib
    mod = importlib.import_module("app.services.query_handlers.aggregation_handler")
    assert hasattr(mod, "handle_aggregation_query")


@pytest.mark.unit
def test_comparison_handler_still_imports():
    """comparison_handler imports without error after switching to adapter."""
    import importlib
    mod = importlib.import_module("app.services.query_handlers.comparison_handler")
    assert hasattr(mod, "handle_comparison_query")
