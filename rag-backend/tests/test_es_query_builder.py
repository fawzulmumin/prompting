"""
Test es_query_builder — Phase 14
Unit tests untuk build_wilayah_filter() termasuk ambiguous multi-kandidat logic.
Jalankan: pytest tests/test_es_query_builder.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import MagicMock

from app.services.core.es_query_builder import build_wilayah_filter
from app.services.core.cognitive_router import CognitiveIntent, WilayahEntities


def _make_intent(**kwargs) -> CognitiveIntent:
    wil = WilayahEntities(**kwargs)
    intent = MagicMock()
    intent.wilayah = wil
    return intent


# ──────────────────────────────────────────────────────────────────────────────
# Non-ambiguous path
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildWilayahFilterNormal:
    def test_kabupaten_returns_term_filter(self):
        intent = _make_intent(kabupaten="kab. bandung")
        filters = build_wilayah_filter(intent)
        assert len(filters) == 1
        assert filters[0] == {"term": {"kabupaten": "kab. bandung"}}

    def test_kecamatan_returns_should_filter(self):
        intent = _make_intent(kecamatan="coblong")
        filters = build_wilayah_filter(intent)
        assert len(filters) == 1
        assert "bool" in filters[0]
        assert "should" in filters[0]["bool"]
        terms_fields = [list(t["term"].keys())[0] for t in filters[0]["bool"]["should"]]
        assert "kecamatan" in terms_fields

    def test_desa_returns_should_filter(self):
        intent = _make_intent(desa="cibunar")
        filters = build_wilayah_filter(intent)
        assert len(filters) == 1
        assert "should" in filters[0]["bool"]
        # Should include kel. variant
        values = [list(t["term"].values())[0] for t in filters[0]["bool"]["should"]]
        assert "cibunar" in values
        assert "kel. cibunar" in values

    def test_provinsi_returns_term_filter(self):
        intent = _make_intent(provinsi="prov. jawa barat")
        filters = build_wilayah_filter(intent)
        assert len(filters) == 1
        assert filters[0] == {"term": {"provinsi": "prov. jawa barat"}}

    def test_provinsi_jakarta_returns_should_filter(self):
        intent = _make_intent(provinsi="prov. d.k.i. jakarta")
        filters = build_wilayah_filter(intent)
        assert len(filters) == 1
        assert "should" in filters[0]["bool"]

    def test_nasional_returns_empty_filters(self):
        intent = _make_intent()  # semua None
        filters = build_wilayah_filter(intent)
        assert filters == []

    def test_hierarki_desa_takes_priority_over_kecamatan(self):
        intent = _make_intent(desa="sukamaju", kecamatan="sukajadi", kabupaten="kab. bandung")
        filters = build_wilayah_filter(intent)
        # Hanya desa yang difilter (bukan kecamatan)
        assert len(filters) == 1
        values = [list(t["term"].values())[0] for t in filters[0]["bool"]["should"]]
        assert "sukamaju" in values


# ──────────────────────────────────────────────────────────────────────────────
# Ambiguous path (Phase 14)
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildWilayahFilterAmbiguous:
    def test_two_candidates_builds_should_query(self):
        intent = _make_intent(
            desa="cibunar",
            ambiguous=True,
            candidates=[
                {"desa_kelurahan": "cibunar", "kecamatan": "parungpanjang",
                 "kabupaten": "kab. bogor", "provinsi": "prov. jawa barat"},
                {"desa_kelurahan": "cibunar", "kecamatan": "cibatu",
                 "kabupaten": "kab. garut", "provinsi": "prov. jawa barat"},
            ]
        )
        filters = build_wilayah_filter(intent)
        assert len(filters) == 1
        bool_clause = filters[0]["bool"]
        assert "should" in bool_clause
        assert len(bool_clause["should"]) == 2
        assert bool_clause["minimum_should_match"] == 1

    def test_ambiguous_empty_candidates_fallback_to_normal(self):
        """ambiguous=True tapi candidates kosong → fallback ke non-ambiguous path."""
        intent = _make_intent(kecamatan="sukajadi", ambiguous=True, candidates=[])
        filters = build_wilayah_filter(intent)
        # Harus fallback ke normal kecamatan filter
        assert len(filters) == 1
        assert "should" in filters[0]["bool"]

    def test_ambiguous_single_candidate_produces_one_should(self):
        intent = _make_intent(
            kecamatan="sukajadi",
            ambiguous=True,
            candidates=[{"kecamatan": "sukajadi", "kabupaten": "kab. bandung"}]
        )
        filters = build_wilayah_filter(intent)
        assert len(filters) == 1
        assert len(filters[0]["bool"]["should"]) == 1

    def test_candidate_with_multiple_levels_builds_nested_bool(self):
        """Kandidat dengan lebih dari 1 level → nested bool.filter."""
        intent = _make_intent(
            desa="cibunar",
            ambiguous=True,
            candidates=[
                {"desa_kelurahan": "cibunar", "kecamatan": "parungpanjang",
                 "kabupaten": "kab. bogor", "provinsi": "prov. jawa barat"},
            ]
        )
        filters = build_wilayah_filter(intent)
        # Satu kandidat dengan 4 level → nested bool.filter
        candidate_clause = filters[0]["bool"]["should"][0]
        assert "bool" in candidate_clause
        assert "filter" in candidate_clause["bool"]
        assert len(candidate_clause["bool"]["filter"]) == 4

    def test_ambiguous_false_with_candidates_ignored(self):
        """ambiguous=False → abaikan candidates, pakai normal path."""
        intent = _make_intent(
            kabupaten="kab. bandung",
            ambiguous=False,
            candidates=[{"kabupaten": "kab. bogor"}],  # should be ignored
        )
        filters = build_wilayah_filter(intent)
        # Normal path: kabupaten filter
        assert len(filters) == 1
        assert filters[0] == {"term": {"kabupaten": "kab. bandung"}}
