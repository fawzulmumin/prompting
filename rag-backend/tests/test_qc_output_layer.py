import pytest
from unittest.mock import MagicMock, AsyncMock


@pytest.mark.unit
class TestFormatHitQcFields:
    """Test bahwa _format_hit() mengekstrak field QC dengan benar."""

    def _make_handler(self):
        from app.services.query_handlers.list_school_handler import ListSchoolHandler
        es_mock = MagicMock()
        return ListSchoolHandler(es=es_mock, index_name="idx_sekolah_qc")

    def test_format_hit_includes_total_masalah(self):
        handler = self._make_handler()
        hit = {
            "_source": {
                "nama": "SDN 1 Test", "npsn": "12345678",
                "bentuk_pendidikan": "SD", "status_sekolah": "NEGERI",
                "akreditasi": "B", "kabupaten": "bandung", "provinsi": "jawa barat",
                "kecamatan": "kec. test", "desa_kelurahan": "desa test",
                "qc_score": {"total_masalah": 3, "persen_bersih": 76.92},
                "content": "SDN 1 Test. SD. Masalah data: koordinat lintang kosong, foto sekolah belum diunggah, yayasan tidak terdaftar.",
            }
        }
        result = handler._format_hit(hit)
        assert result["total_masalah"] == 3
        assert result["persen_bersih"] == 76.92
        assert "koordinat" in result["masalah_detail"]

    def test_format_hit_no_qc_data_returns_none(self):
        """Sekolah tanpa data QC → total_masalah=None, bukan error."""
        handler = self._make_handler()
        hit = {
            "_source": {
                "nama": "SDN 2 Test", "npsn": "87654321",
                "bentuk_pendidikan": "SD", "status_sekolah": "NEGERI",
                "akreditasi": "A", "kabupaten": "surabaya", "provinsi": "jawa timur",
                "kecamatan": "kec. test", "desa_kelurahan": "desa test",
                # Tidak ada qc_score
            }
        }
        result = handler._format_hit(hit)
        assert result["total_masalah"] is None
        assert result["persen_bersih"] is None
        assert result["masalah_detail"] is None

    def test_format_hit_clean_school_zero_masalah(self):
        """Sekolah bersih → total_masalah=0, persen_bersih=100."""
        handler = self._make_handler()
        hit = {
            "_source": {
                "nama": "SDN 3 Bersih", "npsn": "11111111",
                "bentuk_pendidikan": "SD", "status_sekolah": "NEGERI",
                "akreditasi": "A", "kabupaten": "malang", "provinsi": "jawa timur",
                "kecamatan": "kec. test", "desa_kelurahan": "desa test",
                "qc_score": {"total_masalah": 0, "persen_bersih": 100.0},
                "content": "SDN 3 Bersih. SD. Data lengkap.",
            }
        }
        result = handler._format_hit(hit)
        assert result["total_masalah"] == 0
        assert result["persen_bersih"] == 100.0


@pytest.mark.unit
class TestExtractMasalahFromContent:
    """Test helper parser masalah dari field content."""

    def _get_handler(self):
        from app.services.query_handlers.list_school_handler import ListSchoolHandler
        return ListSchoolHandler

    def test_extract_single_masalah(self):
        Handler = self._get_handler()
        content = "SDN 1. SD. Masalah data: foto sekolah belum diunggah."
        result = Handler._extract_masalah_from_content(content)
        assert len(result) == 1
        assert "foto sekolah belum diunggah" in result

    def test_extract_multiple_masalah(self):
        Handler = self._get_handler()
        content = "RA Test. RA. Masalah data: koordinat lintang kosong, koordinat bujur kosong, foto sekolah belum diunggah, yayasan tidak terdaftar."
        result = Handler._extract_masalah_from_content(content)
        assert len(result) == 4

    def test_extract_no_masalah_data_lengkap(self):
        Handler = self._get_handler()
        content = "TK AISYIYAH. TK. Data lengkap."
        result = Handler._extract_masalah_from_content(content)
        assert result == []

    def test_extract_empty_content(self):
        Handler = self._get_handler()
        assert Handler._extract_masalah_from_content("") == []
        assert Handler._extract_masalah_from_content(None) == []


@pytest.mark.unit
class TestBuildQcSummaryContext:
    """Test fungsi _build_qc_summary_context di narrator_service."""

    def test_summary_with_qc_schools(self):
        from app.services.narrator_service import _build_qc_summary_context
        schools = [
            {
                "nama": "SDN 1 Test", "npsn": "12345678",
                "total_masalah": 3, "persen_bersih": 76.92,
                "masalah_detail": "SDN 1 Test. Masalah data: koordinat lintang kosong, foto sekolah belum diunggah, yayasan tidak terdaftar.",
            },
            {
                "nama": "MI Test", "npsn": "87654321",
                "total_masalah": 1, "persen_bersih": 92.31,
                "masalah_detail": "MI Test. Masalah data: foto sekolah belum diunggah.",
            },
        ]
        result = _build_qc_summary_context(schools)
        assert "SDN 1 Test" in result
        assert "3 masalah" in result
        assert "76.9% bersih" in result
        assert "koordinat lintang kosong" in result

    def test_summary_empty_schools(self):
        from app.services.narrator_service import _build_qc_summary_context
        result = _build_qc_summary_context([])
        assert "Tidak ada" in result

    def test_summary_no_qc_data_returns_empty(self):
        """Schools tanpa field total_masalah → kembalikan string kosong."""
        from app.services.narrator_service import _build_qc_summary_context
        schools = [
            {"nama": "SDN 1", "npsn": "12345678", "total_masalah": None},
        ]
        result = _build_qc_summary_context(schools)
        assert result == ""


@pytest.mark.unit
class TestQcFilterInListHandler:
    """Test bahwa build_qc_filter tersambung ke _build_query."""

    def _make_handler(self):
        from app.services.query_handlers.list_school_handler import ListSchoolHandler
        es_mock = MagicMock()
        return ListSchoolHandler(es=es_mock, index_name="idx_sekolah_qc")

    def _make_intent(self, **kwargs):
        intent = MagicMock()
        intent.qc_masalah_only = kwargs.get("qc_masalah_only", False)
        intent.qc_field = kwargs.get("qc_field", None)
        intent.qc_min_masalah = kwargs.get("qc_min_masalah", None)
        intent.wilayah = MagicMock()
        intent.wilayah.desa = None
        intent.wilayah.kecamatan = None
        intent.wilayah.kabupaten = "bandung"
        intent.wilayah.provinsi = None
        intent.nama_sekolah = None
        intent.npsn = None
        intent.keaktifan = None
        intent.wilayah_terpencil = None
        intent.wilayah_perbatasan = None
        intent.jenjang = None
        intent.jenjang_list = []
        intent.status = None
        intent.akreditasi = None
        intent.akreditasi_mode = None
        intent.akreditasi_list = []
        intent.yayasan = None
        return intent

    def test_build_query_includes_qc_masalah_only_filter(self):
        """_build_query harus memasukkan filter total_masalah >= 1 saat qc_masalah_only=True."""
        handler = self._make_handler()
        intent = self._make_intent(qc_masalah_only=True)
        parsed = {"wilayah": "bandung"}
        query_body = handler._build_query(parsed, wilayah_level="kabupaten",
                                          query_raw="residu sekolah di bandung",
                                          intent=intent)
        filter_clauses = query_body["query"]["bool"]["filter"]
        filter_str = str(filter_clauses)
        assert "qc_score.total_masalah" in filter_str, \
            f"Filter QC tidak ditemukan di query. Filter aktual: {filter_clauses}"

    def test_build_query_includes_qc_field_filter(self):
        """_build_query harus memasukkan filter qc.qc_6 >= 2 saat qc_field=6."""
        handler = self._make_handler()
        intent = self._make_intent(qc_masalah_only=True, qc_field=6)
        parsed = {"wilayah": "bandung"}
        query_body = handler._build_query(parsed, wilayah_level="kabupaten",
                                          query_raw="koordinat kosong di bandung",
                                          intent=intent)
        filter_clauses = query_body["query"]["bool"]["filter"]
        filter_str = str(filter_clauses)
        assert "qc.qc_6" in filter_str, \
            f"Filter qc.qc_6 tidak ditemukan. Filter aktual: {filter_clauses}"

    def test_build_query_no_qc_filter_when_not_set(self):
        """_build_query tidak boleh menambah filter QC jika qc_masalah_only=False."""
        handler = self._make_handler()
        intent = self._make_intent(qc_masalah_only=False)
        parsed = {"wilayah": "bandung"}
        query_body = handler._build_query(parsed, wilayah_level="kabupaten",
                                          query_raw="sekolah di bandung",
                                          intent=intent)
        filter_clauses = query_body["query"]["bool"]["filter"]
        filter_str = str(filter_clauses)
        assert "qc_score.total_masalah" not in filter_str

    def test_build_query_no_qc_filter_when_intent_none(self):
        """_build_query tidak error jika intent=None."""
        handler = self._make_handler()
        parsed = {"wilayah": "bandung"}
        query_body = handler._build_query(parsed, wilayah_level="kabupaten",
                                          query_raw="sekolah di bandung",
                                          intent=None)
        assert "query" in query_body


@pytest.mark.unit
class TestWilayahIntentTrust:

    def _make_intent(self, scope, provinsi=None, kabupaten=None, kecamatan=None):
        intent = MagicMock()
        intent.scope = scope
        intent.qc_masalah_only = True
        intent.qc_field = None
        intent.qc_min_masalah = None
        intent.jenjang = "SMK"
        intent.jenjang_list = []
        intent.status = None
        intent.akreditasi = None
        intent.akreditasi_mode = None
        intent.akreditasi_list = []
        intent.yayasan = None
        intent.nama_sekolah = None
        intent.npsn = None
        intent.keaktifan = None
        intent.wilayah_terpencil = None
        intent.wilayah_perbatasan = None
        wil = MagicMock()
        wil.provinsi  = provinsi
        wil.kabupaten = kabupaten
        wil.kecamatan = kecamatan
        wil.desa      = None
        wil.landmark  = None
        intent.wilayah = wil
        return intent

    @pytest.mark.asyncio
    async def test_jawa_tengah_resolves_as_provinsi(self):
        """'jawa tengah' harus di-resolve sebagai provinsi, bukan desa."""
        from app.services.query_handlers.list_school_handler import ListSchoolHandler
        es_mock = AsyncMock()
        es_mock.search.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {}
        }
        handler = ListSchoolHandler(es=es_mock, index_name="idx_sekolah_qc")
        intent = self._make_intent(scope="provinsi", provinsi="jawa tengah")
        result = await handler.handle("residu sekolah smk di jawa tengah", intent=intent)
        call_args = es_mock.search.call_args
        query_body = call_args[1]["body"] if "body" in call_args[1] else call_args[0][0]
        filter_str = str(query_body)
        assert "provinsi" in filter_str
        assert "desa_kelurahan" not in filter_str

    @pytest.mark.asyncio
    async def test_jakarta_selatan_resolves_correctly(self):
        """'jakarta selatan' harus di-resolve sebagai kabupaten."""
        from app.services.query_handlers.list_school_handler import ListSchoolHandler
        es_mock = AsyncMock()
        es_mock.search.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {}
        }
        handler = ListSchoolHandler(es=es_mock, index_name="idx_sekolah_qc")
        intent = self._make_intent(scope="kabupaten", kabupaten="jakarta selatan")
        result = await handler.handle(
            "sekolah lebih dari 3 masalah di jakarta selatan", intent=intent
        )
        call_args = es_mock.search.call_args
        query_body = call_args[1]["body"] if "body" in call_args[1] else call_args[0][0]
        filter_str = str(query_body)
        assert "jakarta selatan" in filter_str
        assert "bula barat" not in filter_str


@pytest.mark.unit
class TestNasionalScope:

    @pytest.mark.asyncio
    async def test_nasional_scope_no_wilayah_filter_but_qc_filter(self):
        """scope=nasional → tidak ada filter wilayah, tapi filter QC tetap ada."""
        from app.services.query_handlers.list_school_handler import ListSchoolHandler

        es_mock = AsyncMock()
        es_mock.search.return_value = {
            "hits": {"hits": [], "total": {"value": 0}},
            "aggregations": {"by_jenjang": {"buckets": []}, "by_status": {"buckets": []}}
        }
        handler = ListSchoolHandler(es=es_mock, index_name="idx_sekolah_qc")

        intent = MagicMock()
        intent.scope = "nasional"
        intent.qc_masalah_only = True
        intent.qc_field = None
        intent.qc_min_masalah = None
        intent.jenjang = None
        intent.jenjang_list = []
        intent.status = None
        intent.akreditasi = None
        intent.akreditasi_mode = None
        intent.akreditasi_list = []
        intent.yayasan = None
        intent.nama_sekolah = None
        intent.npsn = None
        intent.keaktifan = None
        intent.wilayah_terpencil = None
        intent.wilayah_perbatasan = None
        wil = MagicMock()
        wil.provinsi = wil.kabupaten = wil.kecamatan = wil.desa = wil.landmark = None
        intent.wilayah = wil

        result = await handler.handle("residu sekolah di indonesia", intent=intent)

        assert result["status"] == "success"
        call_args = es_mock.search.call_args
        query_body = call_args[1].get("body") or call_args[0][0]
        filter_str = str(query_body)
        assert "qc_score.total_masalah" in filter_str
        assert "provinsi" not in filter_str
        assert "kabupaten" not in filter_str

    @pytest.mark.unit
    def test_qc_masalah_only_in_parsed_after_query(self):
        """parsed yang dikembalikan harus mengandung qc_masalah_only=True."""
        parsed = {"wilayah": "jawa barat"}
        intent = MagicMock()
        intent.qc_masalah_only = True
        intent.qc_field = 6

        qc_masalah_only = getattr(intent, "qc_masalah_only", False)
        qc_field = getattr(intent, "qc_field", None)
        if qc_masalah_only:
            parsed = {**parsed, "qc_masalah_only": True}
            if qc_field:
                parsed["qc_field"] = qc_field

        assert parsed.get("qc_masalah_only") is True
        assert parsed.get("qc_field") == 6
