"""Tests for _generate_suggestions() helper (Phase 25A)."""
import pytest
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class _FakeWilayah:
    provinsi:  Optional[str] = None
    kabupaten: Optional[str] = None
    kecamatan: Optional[str] = None
    desa:      Optional[str] = None
    landmark:  Optional[str] = None


@dataclass
class _FakeIntent:
    module:        str = "aggregation"
    wilayah:       _FakeWilayah = field(default_factory=_FakeWilayah)
    jenjang:       Optional[str] = None
    metric_target: Optional[str] = None


@pytest.mark.unit
def test_generate_suggestions_aggregation_with_jenjang():
    from app.services.core.ask_pipeline import _generate_suggestions

    intent = _FakeIntent(
        module="aggregation",
        wilayah=_FakeWilayah(provinsi="prov. jawa barat"),
        jenjang="SD",
    )
    result = _generate_suggestions(intent, "aggregation")
    assert isinstance(result, list)
    assert len(result) >= 1
    assert len(result) <= 3
    assert any("Jawa Barat" in s for s in result)


@pytest.mark.unit
def test_generate_suggestions_aggregation_guru_metric():
    from app.services.core.ask_pipeline import _generate_suggestions

    intent = _FakeIntent(
        module="aggregation",
        wilayah=_FakeWilayah(provinsi="prov. jawa timur"),
        metric_target="guru_pns",
    )
    result = _generate_suggestions(intent, "aggregation")
    assert len(result) <= 3
    assert any("siswa" in s.lower() for s in result)


@pytest.mark.unit
def test_generate_suggestions_list_module():
    from app.services.core.ask_pipeline import _generate_suggestions

    intent = _FakeIntent(
        module="list",
        wilayah=_FakeWilayah(provinsi="prov. bali"),
        jenjang="SMA",
    )
    result = _generate_suggestions(intent, "list")
    assert isinstance(result, list)
    assert len(result) <= 3
    assert any("Bali" in s for s in result)


@pytest.mark.unit
def test_generate_suggestions_max_3():
    from app.services.core.ask_pipeline import _generate_suggestions

    intent = _FakeIntent(
        module="aggregation",
        wilayah=_FakeWilayah(provinsi="prov. jawa tengah"),
        jenjang="SMK",
        metric_target="guru_honor",
    )
    result = _generate_suggestions(intent, "aggregation")
    assert len(result) <= 3


@pytest.mark.unit
def test_generate_suggestions_no_lokasi():
    from app.services.core.ask_pipeline import _generate_suggestions

    intent = _FakeIntent(module="aggregation", wilayah=_FakeWilayah())
    result = _generate_suggestions(intent, "aggregation")
    assert isinstance(result, list)
    assert len(result) <= 3


@pytest.mark.unit
def test_generate_suggestions_kabupaten_preferred_over_provinsi():
    from app.services.core.ask_pipeline import _generate_suggestions

    intent = _FakeIntent(
        module="aggregation",
        wilayah=_FakeWilayah(provinsi="prov. jawa barat", kabupaten="kab. bogor"),
        jenjang="SD",
    )
    result = _generate_suggestions(intent, "aggregation")
    assert any("Bogor" in s for s in result)
