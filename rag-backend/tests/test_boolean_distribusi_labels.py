"""Tests for _apply_boolean_labels() helper (Phase 26A)."""
import pytest


@pytest.mark.unit
def test_apply_boolean_labels_bos_integer_keys():
    from app.services.formatters.narrator_prompts import _apply_boolean_labels
    distribusi = [
        {"key": 0, "count": 47, "pct": 100.0},
        {"key": 1, "count": 0, "pct": 0.0},
    ]
    result = _apply_boolean_labels(distribusi, "partisipasi_bos")
    assert result[0]["key"] == "Tidak ikut BOS"
    assert result[1]["key"] == "Ikut BOS"


@pytest.mark.unit
def test_apply_boolean_labels_bos_bool_keys():
    from app.services.formatters.narrator_prompts import _apply_boolean_labels
    distribusi = [
        {"key": False, "count": 47, "pct": 100.0},
        {"key": True, "count": 10, "pct": 0.0},
    ]
    result = _apply_boolean_labels(distribusi, "partisipasi_bos")
    assert result[0]["key"] == "Tidak ikut BOS"
    assert result[1]["key"] == "Ikut BOS"


@pytest.mark.unit
def test_apply_boolean_labels_unknown_field_passthrough():
    from app.services.formatters.narrator_prompts import _apply_boolean_labels
    distribusi = [{"key": "SD", "count": 100, "pct": 50.0}]
    result = _apply_boolean_labels(distribusi, "akreditasi")
    assert result[0]["key"] == "SD"


@pytest.mark.unit
def test_apply_boolean_labels_terpencil():
    from app.services.formatters.narrator_prompts import _apply_boolean_labels
    distribusi = [
        {"key": 1, "count": 10, "pct": 20.0},
        {"key": 0, "count": 40, "pct": 80.0},
    ]
    result = _apply_boolean_labels(distribusi, "wilayah_terpencil")
    assert result[0]["key"] == "Terpencil"
    assert result[1]["key"] == "Tidak terpencil"


@pytest.mark.unit
def test_apply_boolean_labels_preserves_count_pct():
    from app.services.formatters.narrator_prompts import _apply_boolean_labels
    distribusi = [{"key": 0, "count": 47, "pct": 100.0}]
    result = _apply_boolean_labels(distribusi, "partisipasi_bos")
    assert result[0]["count"] == 47
    assert result[0]["pct"] == 100.0
