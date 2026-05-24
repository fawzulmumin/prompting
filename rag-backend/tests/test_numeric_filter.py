"""
Phase 23A: Unit tests untuk NumericFilter + build_numeric_filters().
"""
import pytest
from app.services.core.intent_models import NumericFilter, CognitiveIntent, WilayahEntities
from app.services.core.es_query_builder import build_numeric_filters, build_es_query


# ── Helpers ───────────────────────────────────────────────────────────────────

def _intent(**kw) -> CognitiveIntent:
    defaults = dict(module="list", scope="nasional", wilayah=WilayahEntities())
    defaults.update(kw)
    return CognitiveIntent(**defaults)


# ── build_numeric_filters() ───────────────────────────────────────────────────

@pytest.mark.unit
def test_numeric_filter_gt():
    filters = build_numeric_filters([NumericFilter(field="siswa", operator="gt", value=300)])
    assert filters == [{"range": {"siswa_total": {"gt": 300}}}]


@pytest.mark.unit
def test_numeric_filter_lt():
    filters = build_numeric_filters([NumericFilter(field="guru", operator="lt", value=10)])
    assert filters == [{"range": {"guru_total": {"lt": 10}}}]


@pytest.mark.unit
def test_numeric_filter_gte():
    # ruang_kelas terisi di idx_sekolah_profile; total_komputer dihapus (_LOW_COVERAGE_FIELDS)
    filters = build_numeric_filters([NumericFilter(field="ruang_kelas", operator="gte", value=5)])
    assert filters == [{"range": {"ruang_kelas": {"gte": 5}}}]


@pytest.mark.unit
def test_numeric_filter_lte():
    filters = build_numeric_filters([NumericFilter(field="ruang_kelas", operator="lte", value=3)])
    assert filters == [{"range": {"ruang_kelas": {"lte": 3}}}]


@pytest.mark.unit
def test_numeric_filter_between():
    filters = build_numeric_filters([
        NumericFilter(field="siswa", operator="between", value_min=200, value_max=500)
    ])
    assert filters == [{"range": {"siswa_total": {"gte": 200, "lte": 500}}}]


@pytest.mark.unit
def test_numeric_filter_between_partial_min_only():
    filters = build_numeric_filters([
        NumericFilter(field="siswa", operator="between", value_min=100)
    ])
    assert filters == [{"range": {"siswa_total": {"gte": 100}}}]


@pytest.mark.unit
def test_numeric_filter_empty_list():
    assert build_numeric_filters([]) == []


@pytest.mark.unit
def test_numeric_filter_unknown_field_passthrough():
    filters = build_numeric_filters([NumericFilter(field="custom_field", operator="gt", value=10)])
    assert filters == [{"range": {"custom_field": {"gt": 10}}}]


@pytest.mark.unit
def test_numeric_filter_invalid_operator_skipped():
    filters = build_numeric_filters([NumericFilter(field="siswa", operator="invalid", value=100)])
    assert filters == []


@pytest.mark.unit
def test_numeric_filter_no_value_skipped():
    filters = build_numeric_filters([NumericFilter(field="siswa", operator="gt", value=None)])
    assert filters == []


# ── Integrasi: numeric_filter + wilayah + jenjang dalam build_es_query() ─────

@pytest.mark.unit
def test_numeric_filter_combined_with_jenjang_and_wilayah():
    intent = _intent(
        jenjang="SD",
        numeric_filters=[NumericFilter(field="siswa", operator="gt", value=200)],
    )
    es_filters = {"bentuk_pendidikan": "SD"}
    query = build_es_query(intent, es_filters, size=10)
    body = query["query"]["bool"]["filter"]
    # Jenjang filter harus ada
    assert any(f.get("term", {}).get("bentuk_pendidikan") == "SD" for f in body)
    # Numeric filter harus ada
    assert any("range" in f and "siswa_total" in f["range"] for f in body)


@pytest.mark.unit
def test_numeric_filter_no_effect_when_empty():
    intent = _intent(numeric_filters=[])
    es_filters: dict = {}
    query = build_es_query(intent, es_filters, size=10)
    body = query["query"]["bool"]["filter"]
    assert not any("range" in f and any(
        k in f.get("range", {}) for k in ("siswa_total", "guru_total")
    ) for f in body)


# ── build_filters() di aggregation_core menerapkan numeric_filters ────────────

@pytest.mark.unit
def test_aggregation_core_build_filters_applies_numeric_filters():
    from app.services.core.aggregation_core import build_filters
    entities = {
        "numeric_filters": [NumericFilter(field="siswa", operator="gt", value=300)],
    }
    result = build_filters(entities)
    assert any("range" in f and "siswa_total" in f["range"] for f in result)
    range_clause = next(f["range"]["siswa_total"] for f in result if "range" in f and "siswa_total" in f["range"])
    assert range_clause == {"gt": 300}


@pytest.mark.unit
def test_aggregation_core_build_filters_no_spurious_numeric_term():
    """numeric_filters tidak boleh masuk sebagai term filter literal ke ES."""
    from app.services.core.aggregation_core import build_filters
    entities = {
        "numeric_filters": [NumericFilter(field="siswa", operator="gt", value=100)],
    }
    result = build_filters(entities)
    assert not any("term" in f and "numeric_filters" in f.get("term", {}) for f in result)


# ── index selection: numeric_filters → profile index ──────────────────────────

@pytest.mark.unit
def test_numeric_filter_profile_fields_constant_exists():
    """_PROFILE_NUMERIC_FIELDS harus ada dan berisi field siswa/guru."""
    from app.services.core.aggregation_core import _PROFILE_NUMERIC_FIELDS
    assert "siswa_total" in _PROFILE_NUMERIC_FIELDS
    assert "guru_pns" in _PROFILE_NUMERIC_FIELDS
    assert "guru_total" in _PROFILE_NUMERIC_FIELDS
    assert "total_komputer" in _PROFILE_NUMERIC_FIELDS


@pytest.mark.unit
def test_profile_numeric_fields_not_contain_qc_fields():
    """Field QC tidak boleh ada di _PROFILE_NUMERIC_FIELDS."""
    from app.services.core.aggregation_core import _PROFILE_NUMERIC_FIELDS
    assert "qc_score" not in _PROFILE_NUMERIC_FIELDS
    assert "npsn" not in _PROFILE_NUMERIC_FIELDS


@pytest.mark.unit
def test_list_handler_numeric_filter_injection():
    """List handler harus meneruskan numeric_filters ke query builder."""
    from app.services.core.intent_models import NumericFilter
    from app.services.core.es_query_builder import build_numeric_filters, _NUMERIC_FIELD_MAP

    # total_komputer dihapus dari _NUMERIC_FIELD_MAP (field null di ES → _LOW_COVERAGE_FIELDS)
    assert "komputer" not in _NUMERIC_FIELD_MAP

    # Verifikasi field yang valid masih berjalan
    filters = [NumericFilter(field="siswa", operator="gte", value=100)]
    result = build_numeric_filters(filters)
    assert len(result) == 1
    assert result[0] == {"range": {"siswa_total": {"gte": 100}}}


@pytest.mark.unit
def test_guru_p3k_in_field_map():
    from app.services.core.es_query_builder import _NUMERIC_FIELD_MAP
    assert "guru_p3k" in _NUMERIC_FIELD_MAP
    assert _NUMERIC_FIELD_MAP["guru_p3k"] == "guru_p3k"
    assert "guru_pppk" in _NUMERIC_FIELD_MAP
    assert _NUMERIC_FIELD_MAP["guru_pppk"] == "guru_p3k"


@pytest.mark.unit
def test_guru_s1_in_field_map():
    from app.services.core.es_query_builder import _NUMERIC_FIELD_MAP
    assert "guru_s1" in _NUMERIC_FIELD_MAP
    assert _NUMERIC_FIELD_MAP["guru_s1"] == "guru_s1_keatas"


# ── Drilldown index routing: numeric_filters → profile index ──────────────────

@pytest.mark.unit
def test_drilldown_numeric_filter_mapping_forces_profile():
    """
    Field 'siswa' dipetakan ke 'siswa_total' yang ada di _PROFILE_NUMERIC_FIELDS.
    Routing logic drilldown harus mendeteksi ini dan memilih profile index.
    """
    from app.services.core.aggregation_core import _PROFILE_NUMERIC_FIELDS
    from app.services.core.es_query_builder import _NUMERIC_FIELD_MAP
    from app.services.core.intent_models import NumericFilter

    f = NumericFilter(field="siswa", operator="gt", value=300)
    es_field = _NUMERIC_FIELD_MAP.get(
        f.field if hasattr(f, "field") else "",
        f.field if hasattr(f, "field") else "",
    )
    assert es_field == "siswa_total"
    assert es_field in _PROFILE_NUMERIC_FIELDS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_drilldown_uses_profile_index_with_siswa_filter():
    """
    execute_drilldown_query harus memilih idx_sekolah_profile ketika entities
    mengandung numeric_filters pada field profile (siswa > 300).
    Tanpa fix ini drilldown akan query idx_sekolah_qc → data tidak ada → top3 kosong.
    """
    from unittest.mock import AsyncMock
    from app.services.core.aggregation_core import execute_drilldown_query
    from app.services.core.intent_models import NumericFilter
    import os

    mock_es = AsyncMock()
    mock_es.search.return_value = {
        "aggregations": {
            "drilldown": {
                "buckets": [
                    {"key": "kab. bogor", "doc_count": 1200},
                    {"key": "kab. bandung", "doc_count": 900},
                ]
            }
        }
    }

    entities = {
        "provinsi": "prov. jawa barat",
        "bentuk_pendidikan": "SD",
        "numeric_filters": [NumericFilter(field="siswa", operator="gt", value=300)],
    }

    result = await execute_drilldown_query(
        mock_es, entities, drilldown_field="kabupaten", size=3
    )

    assert mock_es.search.called
    call_kwargs = mock_es.search.call_args
    index_used = call_kwargs.kwargs.get("index") or call_kwargs.args[0] if call_kwargs.args else None
    profile_index = os.getenv("ELASTICSEARCH_PROFILE_INDEX", "idx_sekolah_profile")
    assert index_used == profile_index, (
        f"Drilldown harus pakai profile index saat ada numeric_filter siswa, "
        f"tapi pakai: {index_used}"
    )
    assert len(result) == 2
    assert result[0]["name"] == "kab. bogor"
