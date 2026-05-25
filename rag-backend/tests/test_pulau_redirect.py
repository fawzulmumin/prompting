"""Tests for pulau-redirect in _build_resolution() (Phase 26B)."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class _FakeWilayah:
    provinsi:  Optional[str] = None
    kabupaten: Optional[str] = None
    kecamatan: Optional[str] = None
    desa:      Optional[str] = None
    pulau:     Optional[str] = None


@dataclass
class _FakeIntent:
    module: str = "aggregation"
    scope:  Optional[str] = None
    wilayah: _FakeWilayah = field(default_factory=_FakeWilayah)
    metric_target: Optional[str] = None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provinsi_sulawesi_redirects_to_pulau():
    """LLM kadang mengisi provinsi='sulawesi' padahal seharusnya pulau scope."""
    from app.services.query_handlers.aggregation_handler import _build_resolution
    from app.services.core.result_model import ResultStatus

    intent = _FakeIntent(
        scope=None,
        wilayah=_FakeWilayah(provinsi="sulawesi"),
    )
    mock_es = AsyncMock()
    result = await _build_resolution("berapa SMP di Sulawesi?", mock_es, intent, {})

    assert result["metadata"].get("pulau_scope") is True
    assert result["data"]["entities"].get("pulau") == "sulawesi"
    assert result["metadata"].get("trigger") == "provinsi_pulau_redirect"
    mock_es.search.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provinsi_kalimantan_redirects_to_pulau():
    from app.services.query_handlers.aggregation_handler import _build_resolution

    intent = _FakeIntent(
        scope=None,
        wilayah=_FakeWilayah(provinsi="kalimantan"),
    )
    mock_es = AsyncMock()
    result = await _build_resolution("berapa SD di Kalimantan?", mock_es, intent, {})

    assert result["metadata"].get("pulau_scope") is True
    assert result["data"]["entities"].get("pulau") == "kalimantan"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_pulau_empty_wilayah_pulau_extracted_from_query():
    """
    LLM sets scope=pulau but wilayah.pulau is empty.
    _build_resolution() should scan the query text for known pulau names.
    Reproduces: "berapa SMP negeri di Sulawesi dengan guru kurang dari 5?"
    """
    from app.services.query_handlers.aggregation_handler import _build_resolution

    intent = _FakeIntent(
        scope="pulau",
        wilayah=_FakeWilayah(pulau=None),  # LLM left it empty
    )
    mock_es = AsyncMock()
    result = await _build_resolution(
        "berapa SMP negeri di Sulawesi dengan guru kurang dari 5?",
        mock_es, intent, {}
    )

    assert result["metadata"].get("pulau_scope") is True
    assert result["data"]["entities"].get("pulau") == "sulawesi"
    mock_es.search.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scope_pulau_kalimantan_extracted_from_query():
    """Scan fallback also works for 'Kalimantan'."""
    from app.services.query_handlers.aggregation_handler import _build_resolution

    intent = _FakeIntent(scope="pulau", wilayah=_FakeWilayah(pulau=""))
    mock_es = AsyncMock()
    result = await _build_resolution("SD di Kalimantan berapa?", mock_es, intent, {})

    assert result["metadata"].get("pulau_scope") is True
    assert result["data"]["entities"].get("pulau") == "kalimantan"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_valid_provinsi_not_redirected():
    """Provinsi valid (e.g. 'jawa barat') harus tidak di-redirect ke pulau scope."""
    from app.services.query_handlers.aggregation_handler import _build_resolution

    intent = _FakeIntent(
        scope=None,
        wilayah=_FakeWilayah(provinsi="jawa barat"),
    )
    mock_es = AsyncMock()
    mock_es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
    result = await _build_resolution("berapa SD di Jawa Barat?", mock_es, intent, {})

    assert not result["metadata"].get("pulau_scope")
    entities = result["data"]["entities"]
    assert entities.get("provinsi") == "prov. jawa barat"
