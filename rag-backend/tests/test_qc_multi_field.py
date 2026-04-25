"""
Test untuk:
1. build_qc_filter dengan qc_field_list → filter multi-kolom QC
2. _parse_llm_output mengekstrak qc_field_list dengan benar
3. qc_field_list=[] tidak menambah filter apapun
4. CognitiveIntent punya field qc_persen_query
5. _parse_llm_output mengekstrak qc_persen_query: true dengan benar
6. aggregation_handler routing ke _handle_qc_percentage_query
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


# ─────────────────────────────────────────────────────────────────────────────
# Bagian A — qc_field_list di build_qc_filter
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_build_qc_filter_field_list_two_fields():
    """qc_masalah_only=True + qc_field_list=[6,12] → 3 filter total."""
    from app.services.core.es_query_builder import build_qc_filter
    from app.services.core.cognitive_router import CognitiveIntent

    intent = CognitiveIntent(qc_masalah_only=True, qc_field_list=[6, 12])
    filters = build_qc_filter(intent)

    # Filter 1: total_masalah >= 1
    has_total = any(
        f.get("range", {}).get("qc_score.total_masalah") == {"gte": 1}
        for f in filters
    )
    assert has_total, "Filter total_masalah >= 1 harus ada"

    # Filter 2: qc.qc_6 >= 2
    has_qc6 = any("qc.qc_6" in f.get("range", {}) for f in filters)
    assert has_qc6, "Filter qc.qc_6 >= 2 harus ada"

    # Filter 3: qc.qc_12 >= 2
    has_qc12 = any("qc.qc_12" in f.get("range", {}) for f in filters)
    assert has_qc12, "Filter qc.qc_12 >= 2 harus ada"

    assert len(filters) == 3, f"Harus ada 3 filter, got {len(filters)}"


@pytest.mark.unit
def test_build_qc_filter_empty_field_list():
    """qc_field_list=[] tidak menambah filter apapun."""
    from app.services.core.es_query_builder import build_qc_filter
    from app.services.core.cognitive_router import CognitiveIntent

    intent = CognitiveIntent(qc_masalah_only=False, qc_field_list=[])
    filters = build_qc_filter(intent)
    assert filters == [], f"Seharusnya kosong, got {filters}"


@pytest.mark.unit
def test_build_qc_filter_field_list_out_of_range_ignored():
    """qc_field_list dengan nilai di luar 1-13 diabaikan."""
    from app.services.core.es_query_builder import build_qc_filter
    from app.services.core.cognitive_router import CognitiveIntent

    intent = CognitiveIntent(qc_field_list=[0, 6, 14, 99])
    filters = build_qc_filter(intent)

    # Hanya qc_6 yang valid (0, 14, 99 di luar range)
    has_qc6 = any("qc.qc_6" in f.get("range", {}) for f in filters)
    assert has_qc6
    assert len(filters) == 1, f"Hanya qc_6 yang valid, got {len(filters)} filter"


@pytest.mark.unit
def test_parse_llm_output_extracts_qc_field_list():
    """_parse_llm_output harus mengekstrak qc_field_list dari JSON LLM."""
    from app.services.core.cognitive_router import _parse_llm_output

    raw = (
        '{"module":"list","secondary":[],"scope":"provinsi","sub_queries":{},'
        '"entities":{"jenjang":"SMK","jenjang_list":[],"status":"SWASTA","akreditasi":null,'
        '"akreditasi_mode":null,"metric_target":null,"nama_sekolah":null,"npsn":null,'
        '"wilayah":{"provinsi":"jawa barat","kabupaten":null,"kecamatan":null,"desa":null,'
        '"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,'
        '"radius_km":null,"keaktifan":null,"wilayah_terpencil":null,"wilayah_perbatasan":null,'
        '"qc_masalah_only":true,"qc_field":null,"qc_field_list":[6,12],"qc_min_masalah":null,'
        '"sort_by":null,"sort_order":null,"qc_persen_min":null,"qc_persen_max":null,'
        '"qc_persen_query":false},"is_oot":false,"oot_reason":null}'
    )
    intent = _parse_llm_output(raw)
    assert intent is not None
    assert intent.qc_field_list == [6, 12]
    assert intent.qc_masalah_only is True


# ─────────────────────────────────────────────────────────────────────────────
# Bagian B — qc_persen_query di CognitiveIntent & parser
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_cognitive_intent_has_qc_persen_query():
    """CognitiveIntent harus punya field qc_persen_query default False."""
    from app.services.core.cognitive_router import CognitiveIntent

    intent = CognitiveIntent()
    assert hasattr(intent, "qc_persen_query")
    assert intent.qc_persen_query is False


@pytest.mark.unit
def test_parse_llm_output_extracts_qc_persen_query():
    """_parse_llm_output harus mengekstrak qc_persen_query: true."""
    from app.services.core.cognitive_router import _parse_llm_output

    raw = (
        '{"module":"aggregation","secondary":[],"scope":"nasional","sub_queries":{},'
        '"entities":{"jenjang":null,"jenjang_list":[],"status":null,"akreditasi":null,'
        '"akreditasi_mode":null,"metric_target":null,"nama_sekolah":null,"npsn":null,'
        '"wilayah":{"provinsi":null,"kabupaten":null,"kecamatan":null,"desa":null,'
        '"landmark":null},"wilayah_bandingkan":null,"yayasan":null,"koordinat_user":false,'
        '"radius_km":null,"keaktifan":null,"wilayah_terpencil":null,"wilayah_perbatasan":null,'
        '"qc_masalah_only":false,"qc_field":null,"qc_field_list":[],"qc_min_masalah":null,'
        '"sort_by":null,"sort_order":null,"qc_persen_min":null,"qc_persen_max":null,'
        '"qc_persen_query":true},"is_oot":false,"oot_reason":null}'
    )
    intent = _parse_llm_output(raw)
    assert intent is not None
    assert intent.qc_persen_query is True
    assert intent.module == "aggregation"


@pytest.mark.unit
async def test_aggregation_handler_routes_to_qc_percentage():
    """handle_aggregation_query harus memanggil _handle_qc_percentage_query
    ketika intent.qc_persen_query=True."""
    from app.services.core.cognitive_router import CognitiveIntent, WilayahEntities
    from app.services.query_handlers.aggregation_handler import handle_aggregation_query
    from app.services.core.result_model import ResultStatus

    # Mock ES client yang mengembalikan response count fiktif
    mock_es = AsyncMock()
    mock_es.search.return_value = {
        "aggregations": {"total": {"value": 1000}},
        "hits": {"total": {"value": 1000}},
    }

    intent = CognitiveIntent(
        module="aggregation",
        scope="nasional",
        qc_persen_query=True,
        wilayah=WilayahEntities(),
    )

    result = await handle_aggregation_query(
        query="berapa persen sekolah yang bersih",
        es_client=mock_es,
        intent=intent,
    )

    assert result.get("provider") == "ELASTICSEARCH_QC_PERCENTAGE"
    assert result.get("status") == ResultStatus.SUCCESS
    data = result.get("data", {})
    assert data.get("qc_percentage") is True
    assert "total_sekolah" in data
    assert "persen_bersih" in data
    assert "persen_bermasalah" in data


@pytest.mark.unit
def test_narrate_qc_percentage_format():
    """narrate_qc_percentage harus mengembalikan string dengan angka yang benar."""
    from app.services.narrator_service import narrate_qc_percentage

    narasi = narrate_qc_percentage(
        total_sekolah=100000,
        total_bersih=70000,
        total_bermasalah=30000,
        persen_bersih=70.0,
        persen_bermasalah=30.0,
        wilayah_label="seluruh Indonesia",
    )

    assert "100.000" in narasi
    assert "70.000" in narasi
    assert "30.000" in narasi
    assert "70,0%" in narasi
    assert "30,0%" in narasi
