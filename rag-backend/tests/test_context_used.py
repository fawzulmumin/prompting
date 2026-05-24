"""Tests for _format_context_used() helper (Phase 25C)."""
import pytest


@pytest.mark.unit
def test_format_context_used_numeric_filter_gt():
    from app.services.core.ask_pipeline import _format_context_used
    from app.services.core.intent_models import NumericFilter

    entities = {
        "provinsi": "prov. jawa barat",
        "numeric_filters": [NumericFilter(field="siswa", operator="gt", value=300)],
    }
    result = _format_context_used(entities)
    assert "filter:siswa>300" in result
    assert "provinsi:prov. jawa barat" in result
    assert not any("NumericFilter" in s for s in result)


@pytest.mark.unit
def test_format_context_used_numeric_filter_between():
    from app.services.core.ask_pipeline import _format_context_used
    from app.services.core.intent_models import NumericFilter

    entities = {
        "numeric_filters": [
            NumericFilter(field="siswa", operator="between", value_min=100, value_max=500)
        ]
    }
    result = _format_context_used(entities)
    assert "filter:siswa:100-500" in result


@pytest.mark.unit
def test_format_context_used_no_numeric_filters():
    from app.services.core.ask_pipeline import _format_context_used

    entities = {"provinsi": "prov. bali", "bentuk_pendidikan": "SD"}
    result = _format_context_used(entities)
    assert "provinsi:prov. bali" in result
    assert "bentuk_pendidikan:SD" in result
    assert not any("filter" in s for s in result)


@pytest.mark.unit
def test_format_context_used_partisipasi_bos_false():
    from app.services.core.ask_pipeline import _format_context_used

    entities = {"provinsi": "prov. ntt", "partisipasi_bos": False}
    result = _format_context_used(entities)
    assert "bos:tidak" in result
    assert not any("partisipasi_bos" in s for s in result)


@pytest.mark.unit
def test_format_context_used_partisipasi_bos_true():
    from app.services.core.ask_pipeline import _format_context_used

    entities = {"partisipasi_bos": True}
    result = _format_context_used(entities)
    assert "bos:ya" in result


@pytest.mark.unit
def test_format_context_used_empty_numeric_filters_skipped():
    from app.services.core.ask_pipeline import _format_context_used

    entities = {"numeric_filters": [], "provinsi": "prov. papua"}
    result = _format_context_used(entities)
    assert not any("filter" in s for s in result)
    assert "provinsi:prov. papua" in result
