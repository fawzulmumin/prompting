"""
Test untuk:
1. CognitiveIntent memiliki field sort_by, sort_order, qc_persen_min, qc_persen_max
2. _parse_llm_output mengekstrak field baru dengan benar
3. build_qc_filter menghasilkan range query persen_bersih yang benar
4. _detect_sort_mode prioritaskan intent.sort_by di atas keyword detection
"""
import pytest
from app.services.core.cognitive_router import CognitiveIntent, _parse_llm_output
from app.services.core.es_query_builder import build_qc_filter
from app.services.query_handlers.list_school_handler import _detect_sort_mode


@pytest.mark.unit
def test_cognitive_intent_has_sort_fields():
    intent = CognitiveIntent()
    assert hasattr(intent, "sort_by")
    assert hasattr(intent, "sort_order")
    assert hasattr(intent, "qc_persen_min")
    assert hasattr(intent, "qc_persen_max")
    assert intent.sort_by is None
    assert intent.sort_order is None


@pytest.mark.unit
def test_parse_llm_output_sort_fields():
    raw = '{"module":"list","secondary":[],"scope":"provinsi","entities":{"wilayah":{},"sort_by":"total_masalah","sort_order":"desc","qc_masalah_only":true,"qc_persen_min":null,"qc_persen_max":null},"is_oot":false,"oot_reason":null}'
    intent = _parse_llm_output(raw)
    assert intent is not None
    assert intent.sort_by == "total_masalah"
    assert intent.sort_order == "desc"


@pytest.mark.unit
def test_parse_llm_output_persen_max():
    raw = '{"module":"aggregation","secondary":[],"scope":"provinsi","entities":{"wilayah":{},"qc_masalah_only":true,"qc_persen_max":70,"sort_by":null,"sort_order":null},"is_oot":false,"oot_reason":null}'
    intent = _parse_llm_output(raw)
    assert intent is not None
    assert intent.qc_persen_max == 70.0


@pytest.mark.unit
def test_build_qc_filter_persen_max():
    intent = CognitiveIntent(qc_masalah_only=True, qc_persen_max=70.0)
    filters = build_qc_filter(intent)
    persen_filter = next(
        (f for f in filters if "qc_score.persen_bersih" in f.get("range", {})), None
    )
    assert persen_filter is not None
    assert persen_filter["range"]["qc_score.persen_bersih"]["lte"] == 70.0


@pytest.mark.unit
def test_build_qc_filter_persen_range():
    intent = CognitiveIntent(qc_persen_min=50.0, qc_persen_max=80.0)
    filters = build_qc_filter(intent)
    persen_filter = next(
        (f for f in filters if "qc_score.persen_bersih" in f.get("range", {})), None
    )
    assert persen_filter is not None
    r = persen_filter["range"]["qc_score.persen_bersih"]
    assert r["gte"] == 50.0
    assert r["lte"] == 80.0


@pytest.mark.unit
def test_detect_sort_mode_from_intent():
    intent = CognitiveIntent(sort_by="total_masalah", sort_order="desc")
    mode = _detect_sort_mode("tampilkan sekolah", intent=intent)
    assert mode == "bermasalah"


@pytest.mark.unit
def test_detect_sort_mode_keyword_fallback():
    intent = CognitiveIntent()  # sort_by=None
    mode = _detect_sort_mode("tampilkan sekolah paling banyak masalah", intent=intent)
    assert mode == "bermasalah"
