import pytest
import json
from app.services.core.cognitive_router import _parse_llm_output


@pytest.mark.unit
class TestParseQcIntent:
    """Test bahwa _parse_llm_output membaca field QC dengan benar."""

    def _make_raw(self, **overrides) -> str:
        base = {
            "module": "list",
            "secondary": [],
            "scope": "provinsi",
            "sub_queries": {},
            "entities": {
                "jenjang": None, "jenjang_list": [], "status": None,
                "akreditasi": None, "akreditasi_mode": None, "metric_target": None,
                "nama_sekolah": None, "npsn": None,
                "wilayah": {"provinsi": "jawa barat", "kabupaten": None,
                            "kecamatan": None, "desa": None, "landmark": None},
                "wilayah_bandingkan": None, "yayasan": None,
                "koordinat_user": False, "radius_km": None,
                "keaktifan": None, "wilayah_terpencil": None,
                "wilayah_perbatasan": None,
                "qc_masalah_only": False, "qc_field": None, "qc_min_masalah": None,
            },
            "is_oot": False, "oot_reason": None,
        }
        entities = base["entities"]
        entities.update(overrides)
        return json.dumps(base)

    def test_qc_masalah_only_true_parsed(self):
        raw = self._make_raw(qc_masalah_only=True)
        intent = _parse_llm_output(raw)
        assert intent is not None
        assert intent.qc_masalah_only is True

    def test_qc_field_6_parsed(self):
        raw = self._make_raw(qc_masalah_only=True, qc_field=6)
        intent = _parse_llm_output(raw)
        assert intent is not None
        assert intent.qc_field == 6

    def test_qc_min_masalah_parsed(self):
        raw = self._make_raw(qc_masalah_only=True, qc_min_masalah=3)
        intent = _parse_llm_output(raw)
        assert intent is not None
        assert intent.qc_min_masalah == 3

    def test_qc_field_12_foto_parsed(self):
        raw = self._make_raw(qc_masalah_only=True, qc_field=12)
        intent = _parse_llm_output(raw)
        assert intent is not None
        assert intent.qc_field == 12
        assert intent.qc_masalah_only is True

    def test_qc_false_by_default(self):
        """Query non-QC biasa tidak boleh punya qc_masalah_only=True."""
        raw = self._make_raw()
        intent = _parse_llm_output(raw)
        assert intent is not None
        assert intent.qc_masalah_only is False
        assert intent.qc_field is None
        assert intent.qc_min_masalah is None

    def test_qc_field_string_converted_to_int(self):
        """LLM kadang output qc_field sebagai string — harus dikonversi ke int."""
        raw = self._make_raw(qc_masalah_only=True, qc_field="6")
        intent = _parse_llm_output(raw)
        assert intent is not None
        assert intent.qc_field == 6
        assert isinstance(intent.qc_field, int)
