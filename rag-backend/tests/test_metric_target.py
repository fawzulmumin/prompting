"""
tests/test_metric_target.py
Unit tests for build_metric_aggs() in aggregation_core.py.
All tests are pure unit — no ES, no Redis.
"""
import pytest

from app.services.core.aggregation_core import build_metric_aggs


@pytest.mark.unit
def test_siswa_returns_sum_siswa_total():
    result = build_metric_aggs("siswa")
    assert result == {"main_metric": {"sum": {"field": "siswa_total"}}}


@pytest.mark.unit
def test_siswa_l_returns_sum_siswa_l():
    result = build_metric_aggs("siswa_l")
    assert result == {"main_metric": {"sum": {"field": "siswa_l"}}}


@pytest.mark.unit
def test_siswa_p_returns_sum_siswa_p():
    result = build_metric_aggs("siswa_p")
    assert result == {"main_metric": {"sum": {"field": "siswa_p"}}}


@pytest.mark.unit
def test_guru_returns_sum_guru_total():
    result = build_metric_aggs("guru")
    assert result == {"main_metric": {"sum": {"field": "guru_total"}}}


@pytest.mark.unit
def test_guru_pns_returns_sum_guru_pns():
    result = build_metric_aggs("guru_pns")
    assert result == {"main_metric": {"sum": {"field": "guru_pns"}}}


@pytest.mark.unit
def test_guru_honor_returns_sum_guru_honor():
    result = build_metric_aggs("guru_honor")
    assert result == {"main_metric": {"sum": {"field": "guru_honor"}}}


@pytest.mark.unit
def test_guru_sertifikasi_returns_sum_field():
    result = build_metric_aggs("guru_sertifikasi")
    assert result == {"main_metric": {"sum": {"field": "guru_sertifikasi"}}}


@pytest.mark.unit
def test_guru_s1_returns_sum_guru_s1_keatas():
    result = build_metric_aggs("guru_s1")
    assert result == {"main_metric": {"sum": {"field": "guru_s1_keatas"}}}


@pytest.mark.unit
def test_tendik_returns_sum_script():
    result = build_metric_aggs("tendik")
    assert "main_metric" in result
    agg = result["main_metric"]
    assert "sum" in agg
    assert "script" in agg["sum"]
    assert "source" in agg["sum"]["script"]


@pytest.mark.unit
def test_sekolah_returns_value_count_npsn():
    result = build_metric_aggs("sekolah")
    assert result == {"main_metric": {"value_count": {"field": "npsn"}}}


@pytest.mark.unit
def test_none_returns_value_count_npsn():
    result = build_metric_aggs(None)
    assert result == {"main_metric": {"value_count": {"field": "npsn"}}}


@pytest.mark.unit
def test_unknown_metric_falls_back_to_count():
    result = build_metric_aggs("unknown_metric_xyz")
    assert result == {"main_metric": {"value_count": {"field": "npsn"}}}
