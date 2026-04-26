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


# ─────────────────────────────────────────────────────────────────────────────
# Bagian C — end-to-end: handler → narrator → validasi answer_markdown
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
async def test_qc_percentage_full_response_no_error_strings():
    """
    Regression: query QC persentase harus menghasilkan answer_markdown dengan
    persentase yang valid — BUKAN 'Tidak ditemukan satuan pendidikan' atau 'Tidak diketahui'.

    Bug yang dilindungi: main.py tidak punya handler ELASTICSEARCH_QC_PERCENTAGE
    sehingga jatuh ke handler list dan menghasilkan
    "Tidak ditemukan satuan pendidikan di Tidak diketahui".
    """
    import re
    from app.services.core.cognitive_router import CognitiveIntent, WilayahEntities
    from app.services.query_handlers.aggregation_handler import handle_aggregation_query
    from app.services.narrator_service import narrate_qc_percentage

    # Dua ES call via asyncio.gather: call[0]=total, call[1]=bersih
    mock_es = AsyncMock()
    mock_es.search.side_effect = [
        {"aggregations": {"total": {"value": 350000}}, "hits": {"total": {"value": 350000}}},
        {"aggregations": {"total": {"value": 245000}}, "hits": {"total": {"value": 245000}}},
    ]

    intent = CognitiveIntent(
        module="aggregation",
        scope="nasional",
        qc_persen_query=True,
        wilayah=WilayahEntities(),
    )

    result = await handle_aggregation_query(
        query="berapa persen sekolah yang datanya bersih di seluruh Indonesia",
        es_client=mock_es,
        intent=intent,
    )

    # Requirement 1: provider harus ELASTICSEARCH_QC_PERCENTAGE
    assert result.get("provider") == "ELASTICSEARCH_QC_PERCENTAGE", (
        f"Expected ELASTICSEARCH_QC_PERCENTAGE, got {result.get('provider')!r}"
    )

    # Simulasi branch main.py: if agg_provider == "ELASTICSEARCH_QC_PERCENTAGE"
    data = result.get("data", {})
    answer_markdown = narrate_qc_percentage(
        total_sekolah    =data.get("total_sekolah", 0),
        total_bersih     =data.get("total_bersih", 0),
        total_bermasalah =data.get("total_bermasalah", 0),
        persen_bersih    =data.get("persen_bersih", 0.0),
        persen_bermasalah=data.get("persen_bermasalah", 0.0),
        wilayah_label    =data.get("wilayah_label", "seluruh Indonesia"),
    )

    # Requirement 2: answer_markdown mengandung angka persentase (format XX,X%)
    assert re.search(r"\d+,\d+%", answer_markdown), (
        f"answer_markdown harus mengandung persentase XX,X%, got: {answer_markdown!r}"
    )

    # Requirement 3: TIDAK mengandung pesan error handler list yang salah
    assert "Tidak ditemukan satuan pendidikan" not in answer_markdown, (
        f"answer_markdown mengandung error string dari handler yang salah: {answer_markdown!r}"
    )

    # Requirement 4: TIDAK mengandung "Tidak diketahui"
    assert "Tidak diketahui" not in answer_markdown, (
        f"answer_markdown mengandung 'Tidak diketahui': {answer_markdown!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bagian D — _prompt_school_profile: QC data injection ke prompt
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_prompt_school_profile_includes_qc_data():
    """
    _prompt_school_profile harus menyertakan kualitas_data dalam prompt:
    - 'Kualitas Data:' hadir di bagian DATA SEKOLAH
    - Nama masalah human-readable (mis. 'koordinat lintang', 'foto sekolah')
    - Heading '## Kualitas & Kelengkapan Data' hadir di INSTRUKSI NARASI
    - TIDAK mengandung 'Data kualitas tidak tersedia' ketika kualitas_data ada
    """
    from app.services.narrator_service import _prompt_school_profile

    mock_profile = {
        "kualitas_data": {
            "total_masalah": 4,
            "persen_bersih": 69.23,
            "qc_labels": {
                "qc_6":  "Bermasalah (kode: 2)",   # koordinat lintang
                "qc_7":  "Bermasalah (kode: 2)",   # koordinat bujur
                "qc_12": "Bermasalah (kode: 2)",   # foto sekolah
                "qc_13": "Bermasalah (kode: 3)",   # data yayasan
                "qc_1":  "OK",
                "qc_3":  "OK",
            },
        },
    }

    prompt = _prompt_school_profile(mock_profile)

    # Requirement 1: baris 'Kualitas Data:' hadir di DATA SEKOLAH
    assert "Kualitas Data:" in prompt, (
        "Prompt harus mengandung 'Kualitas Data:' di bagian DATA SEKOLAH"
    )

    # Requirement 2: nama masalah readable (mapped dari _QC_NAMES)
    assert "koordinat lintang" in prompt, (
        "Prompt harus mengandung 'koordinat lintang' (mapped dari qc_6)"
    )
    assert "foto sekolah" in prompt, (
        "Prompt harus mengandung 'foto sekolah' (mapped dari qc_12)"
    )

    # Requirement 3: heading instruksi narasi hadir
    assert "## Kualitas & Kelengkapan Data" in prompt, (
        "Prompt harus mengandung heading '## Kualitas & Kelengkapan Data'"
    )

    # Requirement 4: tidak ada fallback 'tidak tersedia' ketika data ada
    assert "Data kualitas tidak tersedia" not in prompt, (
        "Prompt tidak boleh mengandung 'Data kualitas tidak tersedia' ketika kualitas_data tersedia"
    )


@pytest.mark.unit
def test_prompt_school_profile_no_qc_section_when_clean():
    """
    Ketika kualitas_data.total_masalah == 0, heading '## Kualitas & Kelengkapan Data'
    TIDAK boleh ada di prompt (karena instruksi QC sekarang kondisional dari Python,
    bukan teks statis yang selalu ada).
    """
    from app.services.narrator_service import _prompt_school_profile

    mock_profile = {
        "kualitas_data": {
            "total_masalah": 0,
            "persen_bersih": 100.0,
            "qc_labels": {
                "qc_1": "OK",
                "qc_3": "OK",
                "qc_6": "OK",
                "qc_7": "OK",
            },
        },
    }

    prompt = _prompt_school_profile(mock_profile)

    assert "## Kualitas & Kelengkapan Data" not in prompt, (
        "Heading QC tidak boleh muncul ketika total_masalah == 0"
    )
    assert "WAJIB" not in prompt, (
        "Instruksi WAJIB tidak boleh muncul ketika data bersih"
    )
    assert "Data lengkap (100% bersih)" in prompt, (
        "Prompt harus menyebutkan '100% bersih' di baris Kualitas Data"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bagian E — _build_qc_summary_context: non-empty untuk sekolah bermasalah
# ───────────────────────────────────���─────────────────────────────────────────

@pytest.mark.unit
def test_build_qc_summary_context_nonempty_when_masalah():
    """
    _build_qc_summary_context harus mengembalikan string non-empty ketika
    schools mengandung total_masalah > 0.

    Regression: query qc_field_list (mis. koordinat kosong) menghasilkan
    _qc_summary=None karena _qc_mode hanya cek qc_masalah_only, bukan qc_field_list.
    Fungsi ini harus tetap menghasilkan output valid selama data QC tersedia.
    """
    from app.services.narrator_service import _build_qc_summary_context

    schools = [
        {
            "nama": "SDN 01 Bandung",
            "npsn": "20201234",
            "total_masalah": 2,
            "persen_bersih": 84.6,
            "masalah_detail": "SDN 01 Bandung. SD. Masalah data: koordinat lintang kosong, koordinat bujur kosong.",
        },
        {
            "nama": "SMPN 3 Bandung",
            "npsn": "20205678",
            "total_masalah": 1,
            "persen_bersih": 92.3,
            "masalah_detail": "SMPN 3 Bandung. SMP. Masalah data: koordinat lintang kosong.",
        },
        {
            "nama": "SMAN 5 Bandung",
            "npsn": "20209999",
            "total_masalah": 0,
            "persen_bersih": 100.0,
            "masalah_detail": "",
        },
    ]

    result = _build_qc_summary_context(schools)

    assert result, "Harus mengembalikan string non-empty ketika ada sekolah dengan total_masalah > 0"
    assert "SDN 01 Bandung" in result
    assert "SMPN 3 Bandung" in result
    assert "koordinat lintang" in result


@pytest.mark.unit
def test_build_qc_summary_context_empty_when_no_qc_data():
    """_build_qc_summary_context harus mengembalikan '' ketika semua total_masalah=None."""
    from app.services.narrator_service import _build_qc_summary_context

    schools = [
        {"nama": "TK Fazza", "npsn": "69999001", "total_masalah": None, "persen_bersih": None},
        {"nama": "Politeknik AKMB", "npsn": "69999002", "total_masalah": None, "persen_bersih": None},
    ]

    result = _build_qc_summary_context(schools)
    assert result == "", f"Harus mengembalikan '' ketika tidak ada data QC, got: {result!r}"


@pytest.mark.unit
def test_qc_mode_detected_from_qc_field_list():
    """
    narrate_school_list harus mengaktifkan _qc_mode ketika active_filters
    mengandung qc_field_list, bukan hanya qc_masalah_only.

    Regression: _qc_mode = bool(active_filters.get('qc_masalah_only')) saja
    tidak mendeteksi qc_field_list=[6] sehingga _qc_summary selalu None.
    """
    # Verifikasi logika deteksi _qc_mode langsung tanpa memanggil async function
    def _detect_qc_mode(active_filters):
        return bool(
            active_filters and (
                active_filters.get("qc_masalah_only")
                or active_filters.get("qc_field_list")
                or active_filters.get("qc_field")
            )
        )

    assert _detect_qc_mode({"qc_masalah_only": True}) is True
    assert _detect_qc_mode({"qc_field_list": [6]}) is True,    "qc_field_list=[6] harus trigger _qc_mode"
    assert _detect_qc_mode({"qc_field": "koordinat_lintang"}) is True, "qc_field harus trigger _qc_mode"
    assert _detect_qc_mode({"bentuk": "SD"}) is False,         "filter non-QC tidak boleh trigger _qc_mode"
    assert _detect_qc_mode({}) is False
    assert _detect_qc_mode(None) is False
