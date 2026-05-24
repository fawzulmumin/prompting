"""Tests for boolean field filters (Phase 24B-1)."""
import pytest


@pytest.mark.unit
def test_build_boolean_filters_terpencil():
    from app.services.core.es_query_builder import build_boolean_filters
    result = build_boolean_filters({"wilayah_terpencil": True})
    assert result == [{"term": {"wilayah_terpencil": True}}]


@pytest.mark.unit
def test_build_boolean_filters_perbatasan():
    from app.services.core.es_query_builder import build_boolean_filters
    result = build_boolean_filters({"wilayah_perbatasan": True})
    assert result == [{"term": {"wilayah_perbatasan": True}}]


@pytest.mark.unit
def test_build_boolean_filters_empty():
    from app.services.core.es_query_builder import build_boolean_filters
    assert build_boolean_filters({}) == []
    assert build_boolean_filters({"wilayah_terpencil": None}) == []


@pytest.mark.unit
def test_build_boolean_filters_combined():
    from app.services.core.es_query_builder import build_boolean_filters
    result = build_boolean_filters({
        "wilayah_terpencil": True,
        "wilayah_perbatasan": True,
    })
    assert len(result) == 2


@pytest.mark.unit
def test_intent_model_boolean_fields():
    from app.services.core.intent_models import CognitiveIntent
    intent = CognitiveIntent(module="aggregation")
    assert intent.wilayah_terpencil is None
    assert intent.wilayah_perbatasan is None
    intent.wilayah_terpencil = True
    assert intent.wilayah_terpencil is True
