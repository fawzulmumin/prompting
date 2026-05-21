"""
tests/test_pulau_query.py
Unit tests untuk fitur pulau/zona geografis.
All tests are pure unit — no ES, no Redis.
"""
import pytest
from types import SimpleNamespace

from app.services.core.aggregation_core import PULAU_PROVINSI_MAP, build_filters
from app.services.core.intent_models import VALID_SCOPES, WilayahEntities
from app.services.core.es_query_builder import build_wilayah_filter


# ── PULAU_PROVINSI_MAP tests ─────────────────────────────────────────────────

@pytest.mark.unit
def test_pulau_jawa_has_six_provinces():
    assert len(PULAU_PROVINSI_MAP["jawa"]) == 6


@pytest.mark.unit
def test_pulau_jawa_includes_dki():
    assert "prov. dki jakarta" in PULAU_PROVINSI_MAP["jawa"]


@pytest.mark.unit
def test_pulau_sumatera_has_ten_provinces():
    assert len(PULAU_PROVINSI_MAP["sumatera"]) == 10


@pytest.mark.unit
def test_pulau_kalimantan_has_five_provinces():
    assert len(PULAU_PROVINSI_MAP["kalimantan"]) == 5


@pytest.mark.unit
def test_pulau_sulawesi_has_six_provinces():
    assert len(PULAU_PROVINSI_MAP["sulawesi"]) == 6


@pytest.mark.unit
def test_pulau_bali_has_one_province():
    assert PULAU_PROVINSI_MAP["bali"] == ["prov. bali"]


@pytest.mark.unit
def test_all_pulau_keys_present():
    expected_keys = {"jawa", "sumatera", "kalimantan", "sulawesi", "papua",
                     "maluku", "nusa tenggara", "bali", "indonesia timur"}
    assert expected_keys.issubset(PULAU_PROVINSI_MAP.keys())


# ── build_filters pulau tests ─────────────────────────────────────────────────

@pytest.mark.unit
def test_build_filters_pulau_jawa_produces_terms_filter():
    filters = build_filters({"pulau": "jawa"})
    terms_filters = [f for f in filters if "terms" in f and "provinsi" in f.get("terms", {})]
    assert len(terms_filters) == 1
    assert len(terms_filters[0]["terms"]["provinsi"]) == 6


@pytest.mark.unit
def test_build_filters_pulau_kalimantan_produces_terms_filter():
    filters = build_filters({"pulau": "kalimantan"})
    terms_filters = [f for f in filters if "terms" in f and "provinsi" in f.get("terms", {})]
    assert len(terms_filters) == 1
    assert len(terms_filters[0]["terms"]["provinsi"]) == 5


@pytest.mark.unit
def test_build_filters_pulau_unknown_produces_no_provinsi_filter():
    filters = build_filters({"pulau": "atlantis"})
    terms_filters = [f for f in filters if "terms" in f and "provinsi" in f.get("terms", {})]
    assert len(terms_filters) == 0


@pytest.mark.unit
def test_build_filters_provinsi_list_produces_terms_filter():
    prov_list = ["prov. jawa barat", "prov. jawa timur"]
    filters = build_filters({"provinsi_list": prov_list})
    terms_filters = [f for f in filters if "terms" in f and "provinsi" in f.get("terms", {})]
    assert len(terms_filters) == 1
    assert terms_filters[0]["terms"]["provinsi"] == prov_list


@pytest.mark.unit
def test_build_filters_pulau_not_passed_as_term_filter():
    filters = build_filters({"pulau": "jawa", "bentuk_pendidikan": "SD"})
    # "pulau" key must NOT appear as a direct term filter
    term_filters = [f for f in filters if "term" in f and "pulau" in f.get("term", {})]
    assert len(term_filters) == 0


# ── VALID_SCOPES tests ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_valid_scopes_includes_pulau():
    assert "pulau" in VALID_SCOPES


@pytest.mark.unit
def test_valid_scopes_retains_existing():
    for scope in ("nasional", "provinsi", "kabupaten", "kecamatan", "desa", "geo_radius"):
        assert scope in VALID_SCOPES


# ── WilayahEntities pulau field tests ────────────────────────────────────────

@pytest.mark.unit
def test_wilayah_entities_has_pulau_field():
    wil = WilayahEntities()
    assert hasattr(wil, "pulau")
    assert wil.pulau is None


@pytest.mark.unit
def test_wilayah_entities_pulau_set():
    wil = WilayahEntities(pulau="sumatera")
    assert wil.pulau == "sumatera"


@pytest.mark.unit
def test_wilayah_entities_dict_includes_pulau():
    wil = WilayahEntities(pulau="kalimantan")
    assert "pulau" in wil.__dict__
    assert wil.__dict__["pulau"] == "kalimantan"


# ── build_wilayah_filter pulau tests ─────────────────────────────────────────

def _make_intent(pulau=None, provinsi=None, kabupaten=None, kecamatan=None, desa=None):
    wil = WilayahEntities(
        provinsi=provinsi, kabupaten=kabupaten,
        kecamatan=kecamatan, desa=desa, pulau=pulau,
    )
    return SimpleNamespace(wilayah=wil)


@pytest.mark.unit
def test_build_wilayah_filter_pulau_jawa():
    intent = _make_intent(pulau="jawa")
    filters = build_wilayah_filter(intent)
    assert len(filters) == 1
    assert "terms" in filters[0]
    assert len(filters[0]["terms"]["provinsi"]) == 6


@pytest.mark.unit
def test_build_wilayah_filter_pulau_sulawesi():
    intent = _make_intent(pulau="sulawesi")
    filters = build_wilayah_filter(intent)
    assert len(filters) == 1
    assert "terms" in filters[0]
    assert "prov. sulawesi selatan" in filters[0]["terms"]["provinsi"]


@pytest.mark.unit
def test_build_wilayah_filter_provinsi_takes_precedence_over_pulau():
    intent = _make_intent(pulau="jawa", provinsi="prov. jawa barat")
    filters = build_wilayah_filter(intent)
    # provinsi is more specific — should NOT use terms (pulau) filter
    assert not any("terms" in f for f in filters)


@pytest.mark.unit
def test_build_wilayah_filter_no_pulau_no_filter():
    intent = _make_intent()
    filters = build_wilayah_filter(intent)
    assert filters == []
