"""Tests for narrator_prompts utility functions (Phase 24A-2)."""
import pytest


@pytest.mark.unit
def test_format_numeric_filter_text_gt():
    from app.services.formatters.narrator_prompts import format_numeric_filter_text
    from app.services.core.intent_models import NumericFilter

    filters = [NumericFilter(field="siswa", operator="gt", value=300.0)]
    result = format_numeric_filter_text(filters)
    assert "lebih dari" in result
    assert "300" in result
    assert "siswa" in result


@pytest.mark.unit
def test_format_numeric_filter_text_gte():
    from app.services.formatters.narrator_prompts import format_numeric_filter_text
    from app.services.core.intent_models import NumericFilter

    filters = [NumericFilter(field="guru_pns", operator="gte", value=5.0)]
    result = format_numeric_filter_text(filters)
    assert "minimal" in result
    assert "5" in result
    assert "PNS" in result or "guru" in result.lower()


@pytest.mark.unit
def test_format_numeric_filter_text_between():
    from app.services.formatters.narrator_prompts import format_numeric_filter_text
    from app.services.core.intent_models import NumericFilter

    filters = [NumericFilter(field="siswa", operator="between", value_min=100.0, value_max=500.0)]
    result = format_numeric_filter_text(filters)
    assert "antara" in result
    assert "100" in result
    assert "500" in result


@pytest.mark.unit
def test_format_numeric_filter_text_empty():
    from app.services.formatters.narrator_prompts import format_numeric_filter_text

    assert format_numeric_filter_text([]) == ""
    assert format_numeric_filter_text(None) == ""


@pytest.mark.unit
def test_format_numeric_filter_text_dict_input():
    from app.services.formatters.narrator_prompts import format_numeric_filter_text

    filters = [{"field": "siswa", "operator": "lt", "value": 100.0}]
    result = format_numeric_filter_text(filters)
    assert "kurang dari" in result
    assert "100" in result
    assert "siswa" in result


@pytest.mark.unit
def test_format_numeric_filter_integer_value():
    from app.services.formatters.narrator_prompts import format_numeric_filter_text
    from app.services.core.intent_models import NumericFilter

    filters = [NumericFilter(field="guru_honor", operator="lte", value=10.0)]
    result = format_numeric_filter_text(filters)
    assert "maksimal" in result
    assert "10" in result
    assert "honorer" in result
