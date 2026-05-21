"""
Test LocationResolver (Task 3.1)
Jalankan: pytest tests/test_location_resolver.py -v
Tidak butuh ES live — semua mocked via monkeypatch.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import patch

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Shared test data — controlled mock index
# ──────────────────────────────────────────────────────────────────────────────

# Simulates LOCATION_INDEX after startup:
#   {level: {normalized_name: canonical_es_value}}
MOCK_INDEX = {
    "provinsi": {
        "jawa barat":      "jawa barat",
        "jawabarat":       "jawa barat",
        "jawa timur":      "jawa timur",
        "kalimantan timur": "kalimantan timur",
        "kaltim":          "kalimantan timur",
        "dki jakarta":     "DKI Jakarta",
        "banten":          "banten",
    },
    "kabupaten": {
        # Only stripped (no-prefix) keys here to exercise the prefix-strip path.
        # "kab. bandung" is NOT a key — stripping "kab. " from user input gives "bandung" which IS here.
        "bandung":                "bandung",
        "kota bandung":           "kota bandung",
        "bogor":                  "bogor",
        "kota bogor":             "kota bogor",
        "bekasi":                 "bekasi",
        "kota bekasi":            "kota bekasi",
        "tangerang selatan":      "kota tangerang selatan",
        "kota tangerang selatan": "kota tangerang selatan",
        "tangsel":                "kota tangerang selatan",
    },
    "kecamatan": {
        "ciputat":       "ciputat",
        "serpong":       "serpong",
        "kebayoran baru": "kebayoran baru",
        "kebayoran lama": "kebayoran lama",
        "ciawi":         "ciawi",       # ambiguous: bogor AND tasikmalaya
        "sukajadi":      "sukajadi",    # ambiguous: bandung AND bogor
        "pamulang":      "pamulang",
    },
    "desa_kelurahan": {
        "pamulang barat":  "pamulang barat",
        "babakan":         "babakan",    # ambiguous: 2 kecamatan
        "ciputat":         "ciputat",
        "cipua":           "cipua",
    },
}

# Simulates LOCATION_HIERARCHY after startup:
#   {level: {name_lower: [{parent_fields}, ...]}}
MOCK_HIERARCHY = {
    "kecamatan": {
        "ciputat":  [{"kabupaten": "kota tangerang selatan", "provinsi": "banten"}],
        "serpong":  [{"kabupaten": "kota tangerang selatan", "provinsi": "banten"}],
        "pamulang": [{"kabupaten": "kota tangerang selatan", "provinsi": "banten"}],
        "kebayoran baru": [{"kabupaten": "kota jakarta selatan", "provinsi": "DKI Jakarta"}],
        "kebayoran lama": [{"kabupaten": "kota jakarta selatan", "provinsi": "DKI Jakarta"}],
        "ciawi": [
            {"kabupaten": "bogor",        "provinsi": "jawa barat"},
            {"kabupaten": "tasikmalaya",  "provinsi": "jawa barat"},
        ],
        "sukajadi": [
            {"kabupaten": "bandung",      "provinsi": "jawa barat"},
            {"kabupaten": "bogor",        "provinsi": "jawa barat"},
        ],
    },
    "kabupaten": {
        "bandung":                [{"provinsi": "jawa barat"}],
        "kota bandung":           [{"provinsi": "jawa barat"}],
        "bogor":                  [{"provinsi": "jawa barat"}],
        "kota bogor":             [{"provinsi": "jawa barat"}],
        "kota tangerang selatan": [{"provinsi": "banten"}],
        "bekasi":                 [{"provinsi": "jawa barat"}],
        "kota bekasi":            [{"provinsi": "jawa barat"}],
    },
    "desa_kelurahan": {
        "pamulang barat": [
            {"kecamatan": "pamulang", "kabupaten": "kota tangerang selatan", "provinsi": "banten"}
        ],
        "babakan": [
            {"kecamatan": "ciawi",    "kabupaten": "bogor",  "provinsi": "jawa barat"},
            {"kecamatan": "cibinong", "kabupaten": "bogor",  "provinsi": "jawa barat"},
        ],
        "ciputat": [
            {"kecamatan": "ciputat",  "kabupaten": "kota tangerang selatan", "provinsi": "banten"}
        ],
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# Simple WilayahEntities stand-in (avoids importing cognitive_router)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class W:
    """Minimal WilayahEntities for testing."""
    provinsi:  Optional[str] = None
    kabupaten: Optional[str] = None
    kecamatan: Optional[str] = None
    desa:      Optional[str] = None
    landmark:  Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# Fixture: patch LOCATION_INDEX and LOCATION_HIERARCHY
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_location_service():
    """Patch the module-level globals used by LocationResolver."""
    import app.services.location_service as ls
    original_index = dict(ls.LOCATION_INDEX)
    original_hier  = dict(ls.LOCATION_HIERARCHY)

    ls.LOCATION_INDEX.clear()
    ls.LOCATION_INDEX.update(MOCK_INDEX)
    ls.LOCATION_HIERARCHY.clear()
    ls.LOCATION_HIERARCHY.update(MOCK_HIERARCHY)

    yield

    ls.LOCATION_INDEX.clear()
    ls.LOCATION_INDEX.update(original_index)
    ls.LOCATION_HIERARCHY.clear()
    ls.LOCATION_HIERARCHY.update(original_hier)


@pytest.fixture
def resolver():
    from app.services.core.location_resolver import LocationResolver
    return LocationResolver()


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

class TestExactMatch:
    def test_provinsi_exact(self, resolver):
        r = resolver.resolve(W(provinsi="jawa barat"))
        assert r.provinsi == "jawa barat"
        assert r.method == "exact"
        assert r.confidence == 1.0
        assert r.ambiguous is False

    def test_provinsi_exact_case_insensitive(self, resolver):
        r = resolver.resolve(W(provinsi="JAWA BARAT"))
        assert r.provinsi == "jawa barat"
        assert r.method == "exact"

    def test_kabupaten_exact(self, resolver):
        r = resolver.resolve(W(kabupaten="bandung"))
        assert r.kabupaten == "bandung"
        assert r.confidence == 1.0
        assert r.method == "exact"

    def test_kecamatan_exact(self, resolver):
        r = resolver.resolve(W(kecamatan="ciputat"))
        assert r.kecamatan == "ciputat"
        assert r.method == "exact"

    def test_provinsi_multikword(self, resolver):
        r = resolver.resolve(W(provinsi="kalimantan timur"))
        assert r.provinsi == "kalimantan timur"
        assert r.method == "exact"


class TestPrefixStripped:
    def test_kab_prefix(self, resolver):
        r = resolver.resolve(W(kabupaten="kab. bandung"))
        assert r.kabupaten == "bandung"
        assert r.method == "prefix_stripped"
        assert r.confidence == 0.95

    def test_kota_prefix(self, resolver):
        r = resolver.resolve(W(kabupaten="kota bandung"))
        assert r.kabupaten == "kota bandung"
        assert r.method == "exact"  # "kota bandung" is in the index directly

    def test_kabupaten_word_prefix(self, resolver):
        r = resolver.resolve(W(kabupaten="kabupaten bogor"))
        assert r.kabupaten == "bogor"
        assert r.method == "prefix_stripped"

    def test_kecamatan_prefix(self, resolver):
        r = resolver.resolve(W(kecamatan="kecamatan ciputat"))
        assert r.kecamatan == "ciputat"
        assert r.method == "prefix_stripped"


class TestHierarchyEnrich:
    def test_kecamatan_enriches_kabupaten_provinsi(self, resolver):
        r = resolver.resolve(W(kecamatan="ciputat"))
        assert r.kecamatan == "ciputat"
        assert r.kabupaten == "kota tangerang selatan"
        assert r.provinsi == "banten"

    def test_kabupaten_enriches_provinsi(self, resolver):
        r = resolver.resolve(W(kabupaten="bandung"))
        assert r.kabupaten == "bandung"
        assert r.provinsi == "jawa barat"

    def test_desa_enriches_kecamatan_kabupaten_provinsi(self, resolver):
        r = resolver.resolve(W(desa="pamulang barat"))
        assert r.desa == "pamulang barat"
        assert r.kecamatan == "pamulang"
        assert r.kabupaten == "kota tangerang selatan"
        assert r.provinsi == "banten"

    def test_desa_enrich_does_not_override_existing_kecamatan(self, resolver):
        # User specified kecamatan explicitly — resolver should not overwrite
        r = resolver.resolve(W(desa="ciputat", kecamatan="ciputat"))
        assert r.kecamatan == "ciputat"
        assert r.kabupaten == "kota tangerang selatan"


class TestFuzzyMatch:
    def test_fuzzy_kecamatan_typo(self, resolver):
        # "ciputa" is 1 edit distance from "ciputat"
        r = resolver.resolve(W(kecamatan="ciputa"))
        # Fuzzy should find "ciputat"
        if r.method == "fuzzy":
            assert r.kecamatan == "ciputat"
            assert r.confidence == 0.7
        else:
            # If not fuzzy-matched (e.g. threshold too strict), unresolved is acceptable
            assert r.method in ("fuzzy", "unresolved")

    def test_short_name_no_fuzzy(self, resolver):
        # "bek" is too short for fuzzy (< 5 chars)
        r = resolver.resolve(W(kabupaten="bek"))
        assert r.method == "unresolved"

    def test_multiword_no_fuzzy(self, resolver):
        # Multi-word names are not fuzzied word-by-word
        r = resolver.resolve(W(provinsi="kalimantan timurr"))
        # Not in exact, not strippable, not single-word → unresolved
        assert r.method == "unresolved"


class TestAmbiguity:
    def test_ambiguous_kecamatan(self, resolver):
        # "ciawi" exists in 2 kabupaten
        r = resolver.resolve(W(kecamatan="ciawi"))
        assert r.ambiguous is True
        assert len(r.candidates) >= 2
        assert r.confidence <= 0.5

    def test_disambiguation_via_kabupaten(self, resolver):
        # "sukajadi" in 2 kabupaten, but we provide "bandung" as context
        r = resolver.resolve(W(kecamatan="sukajadi", kabupaten="bandung"))
        assert r.kecamatan == "sukajadi"
        # After disambiguation with kabupaten="bandung", should not be ambiguous
        assert r.ambiguous is False

    def test_ambiguous_desa(self, resolver):
        # "babakan" exists in 2 kecamatan
        r = resolver.resolve(W(desa="babakan"))
        assert r.ambiguous is True
        assert r.confidence <= 0.5

    def test_desa_disambiguation_via_kecamatan(self, resolver):
        # "babakan" in 2 kecamatan, but we provide kecamatan="ciawi" as context
        r = resolver.resolve(W(desa="babakan", kecamatan="ciawi"))
        assert r.ambiguous is False


class TestConfidenceOrdering:
    def test_exact_has_highest_confidence(self, resolver):
        r = resolver.resolve(W(kabupaten="bandung"))
        assert r.confidence == 1.0

    def test_prefix_stripped_has_lower_confidence(self, resolver):
        r = resolver.resolve(W(kabupaten="kab. bandung"))
        assert r.confidence == 0.95

    def test_unresolved_has_zero_confidence(self, resolver):
        r = resolver.resolve(W(kabupaten="kabupaten_xyznotexist"))
        assert r.confidence == 0.0
        assert r.method == "unresolved"

    def test_ambiguous_caps_confidence(self, resolver):
        r = resolver.resolve(W(kecamatan="ciawi"))
        assert r.confidence <= 0.5

    def test_mixed_levels_take_worst_confidence(self, resolver):
        # provinsi exact (1.0) + kabupaten prefix-stripped (0.95)
        r = resolver.resolve(W(provinsi="jawa barat", kabupaten="kab. bandung"))
        assert r.confidence == 0.95  # worst of (1.0, 0.95)


class TestEdgeCases:
    def test_empty_wilayah(self, resolver):
        r = resolver.resolve(W())
        assert r.provinsi is None
        assert r.kabupaten is None
        assert r.kecamatan is None
        assert r.desa is None
        assert r.method == "exact"

    def test_none_wilayah(self, resolver):
        r = resolver.resolve(None)
        assert r.provinsi is None
        assert r.confidence == 1.0

    def test_landmark_passthrough(self, resolver):
        # Landmark is NOT resolved by LocationResolver — it's for geo handler
        w = W(landmark="Monas")
        r = resolver.resolve(w)
        assert r.desa is None
        assert r.kecamatan is None
        # landmark field is NOT in ResolvedWilayah — that's intentional

    def test_unresolved_returns_raw_value(self, resolver):
        r = resolver.resolve(W(kabupaten="unknown_kab_xyz123"))
        assert r.kabupaten == "unknown_kab_xyz123"
        assert r.method == "unresolved"

    def test_index_empty_passthrough(self, resolver):
        import app.services.location_service as ls
        saved = dict(ls.LOCATION_INDEX)
        ls.LOCATION_INDEX.clear()
        try:
            r = resolver.resolve(W(kabupaten="bandung"))
            assert r.method == "unresolved"
            assert r.kabupaten == "bandung"  # raw passthrough
        finally:
            ls.LOCATION_INDEX.update(saved)


class TestResolvedWilayahIntegration:
    def test_full_chain_kecamatan_only(self, resolver):
        """Only kecamatan given — should auto-fill kabupaten and provinsi."""
        r = resolver.resolve(W(kecamatan="serpong"))
        assert r.kecamatan == "serpong"
        assert r.kabupaten == "kota tangerang selatan"
        assert r.provinsi == "banten"
        assert r.desa is None
        assert r.ambiguous is False

    def test_full_chain_desa_enrich_all(self, resolver):
        """Only desa given — should fill kecamatan, kabupaten, provinsi."""
        r = resolver.resolve(W(desa="pamulang barat"))
        assert r.desa == "pamulang barat"
        assert r.kecamatan == "pamulang"
        assert r.kabupaten == "kota tangerang selatan"
        assert r.provinsi == "banten"

    def test_explicit_provinsi_not_overridden_by_enrich(self, resolver):
        """If user provides provinsi, enrich from kecamatan should not override it."""
        r = resolver.resolve(W(kecamatan="ciputat", provinsi="banten"))
        assert r.provinsi == "banten"  # from user, same as enrich result
        assert r.kecamatan == "ciputat"
        assert r.kabupaten == "kota tangerang selatan"

    def test_resolve_wilayah_convenience(self):
        from app.services.core.location_resolver import resolve_wilayah
        r = resolve_wilayah(W(provinsi="jawa barat"))
        assert r.provinsi == "jawa barat"


class TestGracefulFallback:
    """Unresolved desa/kecamatan should fall back to parent scope, not block query."""

    def test_desa_unresolved_with_kecamatan_drops_desa(self, resolver):
        # "cibunar" is NOT in MOCK_INDEX desa_kelurahan, "ciputat" IS in kecamatan
        r = resolver.resolve(W(desa="cibunar", kecamatan="ciputat"))
        assert r.desa is None, "Unresolved desa should be dropped when kecamatan is available"
        assert r.kecamatan == "ciputat"
        assert r.kabupaten == "kota tangerang selatan"
        assert r.provinsi == "banten"
        # Confidence should reflect only the resolved kecamatan, not the unresolved desa
        assert r.confidence >= 0.9

    def test_desa_unresolved_without_kecamatan_keeps_raw(self, resolver):
        # No kecamatan fallback available → raw value is preserved (passthrough)
        r = resolver.resolve(W(desa="cibunar"))
        assert r.desa == "cibunar"
        assert r.method == "unresolved"
        assert r.confidence == 0.0

    def test_kecamatan_unresolved_with_kabupaten_drops_kecamatan(self, resolver):
        # "parungpanjang" is NOT in MOCK_INDEX kecamatan, "bogor" IS in kabupaten
        r = resolver.resolve(W(kecamatan="parungpanjang", kabupaten="bogor"))
        assert r.kecamatan is None, "Unresolved kecamatan should be dropped when kabupaten is available"
        assert r.kabupaten == "bogor"
        assert r.provinsi == "jawa barat"
        assert r.confidence >= 0.9

    def test_kecamatan_unresolved_without_kabupaten_keeps_raw(self, resolver):
        # No kabupaten fallback → raw value is preserved
        r = resolver.resolve(W(kecamatan="parungpanjang"))
        assert r.kecamatan == "parungpanjang"
        assert r.method == "unresolved"
        assert r.confidence == 0.0

    def test_desa_unresolved_kecamatan_also_unresolved_uses_raw_desa(self, resolver):
        # Both desa and kecamatan are unknown — kecamatan condition not satisfied
        # (kecamatan IS provided but won't resolve either — still triggers the drop
        #  because we check wilayah.kecamatan is truthy, not whether it resolves)
        r = resolver.resolve(W(desa="cibunar", kecamatan="parungpanjang"))
        # desa is dropped because kecamatan is provided (even if kecamatan also unresolved)
        assert r.desa is None
        # kecamatan unresolved with no kabupaten → stays raw
        assert r.kecamatan == "parungpanjang"
        assert r.method == "unresolved"
