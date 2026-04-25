import pytest
from unittest.mock import MagicMock


@pytest.mark.unit
class TestBuildQcFilter:

    def _make_intent(self, **kwargs):
        """Helper: buat mock intent dengan nilai default."""
        intent = MagicMock()
        intent.qc_masalah_only = kwargs.get("qc_masalah_only", False)
        intent.qc_field = kwargs.get("qc_field", None)
        intent.qc_min_masalah = kwargs.get("qc_min_masalah", None)
        return intent

    def test_empty_when_no_qc_flags(self):
        """Jika tidak ada flag QC, kembalikan list kosong."""
        from app.services.core.es_query_builder import build_qc_filter
        intent = self._make_intent()
        result = build_qc_filter(intent)
        assert result == []

    def test_qc_masalah_only_true(self):
        """qc_masalah_only=True → total_masalah >= 1."""
        from app.services.core.es_query_builder import build_qc_filter
        intent = self._make_intent(qc_masalah_only=True)
        result = build_qc_filter(intent)
        assert len(result) == 1
        assert result[0] == {"range": {"qc_score.total_masalah": {"gte": 1}}}

    def test_qc_min_masalah(self):
        """qc_min_masalah=3 → total_masalah >= 3."""
        from app.services.core.es_query_builder import build_qc_filter
        intent = self._make_intent(qc_min_masalah=3)
        result = build_qc_filter(intent)
        assert any(
            f.get("range", {}).get("qc_score.total_masalah", {}).get("gte") == 3
            for f in result
        )

    def test_qc_masalah_only_and_min_masalah_takes_max(self):
        """qc_masalah_only=True + qc_min_masalah=5 → ambil nilai terbesar (5)."""
        from app.services.core.es_query_builder import build_qc_filter
        intent = self._make_intent(qc_masalah_only=True, qc_min_masalah=5)
        result = build_qc_filter(intent)
        total_filter = next(
            f for f in result if "qc_score.total_masalah" in f.get("range", {})
        )
        assert total_filter["range"]["qc_score.total_masalah"]["gte"] == 5

    def test_qc_field_6_koordinat(self):
        """qc_field=6 → filter qc.qc_6 >= 2 (koordinat lintang bermasalah)."""
        from app.services.core.es_query_builder import build_qc_filter
        intent = self._make_intent(qc_masalah_only=True, qc_field=6)
        result = build_qc_filter(intent)
        field_filter = next(
            (f for f in result if "qc.qc_6" in f.get("range", {})), None
        )
        assert field_filter is not None
        assert field_filter["range"]["qc.qc_6"]["gte"] == 2

    def test_qc_field_12_foto(self):
        """qc_field=12 → filter qc.qc_12 >= 2 (foto sekolah bermasalah)."""
        from app.services.core.es_query_builder import build_qc_filter
        intent = self._make_intent(qc_masalah_only=True, qc_field=12)
        result = build_qc_filter(intent)
        field_filter = next(
            (f for f in result if "qc.qc_12" in f.get("range", {})), None
        )
        assert field_filter is not None

    def test_qc_field_out_of_range_ignored(self):
        """qc_field di luar 1-15 → diabaikan."""
        from app.services.core.es_query_builder import build_qc_filter
        intent = self._make_intent(qc_masalah_only=False, qc_field=99)
        result = build_qc_filter(intent)
        assert all("qc.qc_" not in str(f) for f in result)

    def test_combined_masalah_only_and_field(self):
        """qc_masalah_only=True + qc_field=13 → dua filter sekaligus."""
        from app.services.core.es_query_builder import build_qc_filter
        intent = self._make_intent(qc_masalah_only=True, qc_field=13)
        result = build_qc_filter(intent)
        assert len(result) == 2


@pytest.mark.unit
class TestIntentClassifierResiduKeywords:

    def test_residu_keyword_classified_as_school(self):
        """Kata 'residu' + 'sekolah' harus → SCHOOL bukan OOC."""
        from app.services.core.intent_classifier import classify_intent_rule, IntentType
        result = classify_intent_rule("residu sekolah SMK di Jakarta")
        assert result == IntentType.SCHOOL

    def test_data_bermasalah_classified_as_school(self):
        from app.services.core.intent_classifier import classify_intent_rule, IntentType
        result = classify_intent_rule("data bermasalah di Jawa Barat")
        assert result == IntentType.SCHOOL

    def test_koordinat_kosong_classified_as_school(self):
        from app.services.core.intent_classifier import classify_intent_rule, IntentType
        result = classify_intent_rule("sekolah koordinat kosong di Bandung")
        assert result == IntentType.SCHOOL

    def test_foto_belum_classified_as_school(self):
        from app.services.core.intent_classifier import classify_intent_rule, IntentType
        result = classify_intent_rule("foto sekolah belum diunggah di Surabaya")
        assert result == IntentType.SCHOOL
