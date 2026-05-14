"""
Test IntentClassifier (multi-tier)
Jalankan: pytest tests/test_intent_classifier.py -v -s
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app.classification.intent_classifier import (
    IntentClassifier,
    ClassificationResult,
    classify_intent_sync,
)


@pytest.fixture(scope="module")
def clf():
    return IntentClassifier()


class TestSyncClassifier:
    """Test classify_sync (Tier 1+2 only, no LLM needed)."""

    def test_returns_classification_result(self, clf):
        r = clf.classify_sync("berapa SD di bandung")
        assert isinstance(r, ClassificationResult)

    def test_aggregation_tier1(self, clf):
        r = clf.classify_sync("berapa SD di jawa barat")
        assert r.module == "aggregation"
        assert r.tier_used == "tier1_pattern"
        assert r.confidence >= 0.70

    def test_list_tier1(self, clf):
        r = clf.classify_sync("daftar SMA negeri di jakarta")
        assert r.module == "list"
        assert r.tier_used == "tier1_pattern"

    def test_comparison_tier1(self, clf):
        r = clf.classify_sync("bandingkan SMK di Bandung vs Bekasi")
        assert r.module == "comparison"
        assert r.tier_used == "tier1_pattern"

    def test_geo_tier1(self, clf):
        r = clf.classify_sync("sekolah terdekat dari monas")
        assert r.module == "geo"
        assert r.tier_used == "tier1_pattern"

    def test_oot_tier1(self, clf):
        r = clf.classify_sync("siapa presiden Indonesia")
        assert r.module == "out_of_context"
        assert r.tier_used == "tier1_pattern"

    def test_profile_tier2_fallback(self, clf):
        r = clf.classify_sync("alamat SMAN 3 Bandung")
        assert r.module == "profile"

    def test_list_tier2_fallback(self, clf):
        r = clf.classify_sync("tampilkan sekolah di bekasi")
        assert r.module == "list"

    def test_unknown_defers_gracefully(self, clf):
        r = clf.classify_sync("xyzabc tidak jelas")
        assert isinstance(r, ClassificationResult)
        assert r.module is not None  # falls back to "unknown" string, never None

    def test_duration_populated(self, clf):
        r = clf.classify_sync("berapa SD di bandung")
        assert r.duration_ms > 0
        assert r.duration_ms < 100  # sync must be fast

    def test_fallback_chain_on_tier2(self, clf):
        r = clf.classify_sync("SMAN 3 Bandung info lengkap")
        if r.tier_used == "tier2_rule":
            assert "tier1_defer" in r.fallback_chain

    def test_raw_result_populated(self, clf):
        r = clf.classify_sync("berapa SMP di surabaya")
        assert r.raw_result is not None

    def test_npsn_is_profile(self, clf):
        r = clf.classify_sync("20217725")
        assert r.module == "profile"
        assert r.tier_used == "tier1_pattern"

    def test_vs_is_comparison(self, clf):
        r = clf.classify_sync("SMA di Bandung vs Bekasi mana lebih banyak")
        assert r.module == "comparison"


class TestConvenienceFunctions:
    def test_classify_intent_sync_returns_result(self):
        r = classify_intent_sync("berapa SD di bandung")
        assert isinstance(r, ClassificationResult)
        assert r.module == "aggregation"

    def test_classify_intent_sync_oot(self):
        r = classify_intent_sync("harga laptop gaming 2025")
        assert r.module == "out_of_context"


class TestGoldenSetSyncCoverage:
    """Coverage report: Tier 1+2 via IntentClassifier.classify_sync."""

    def test_sync_coverage_report(self, clf):
        import yaml
        golden_path = Path(__file__).parent / "golden_set" / "golden_set_v1.yaml"
        if not golden_path.exists():
            pytest.skip("Golden set tidak ditemukan")

        with open(golden_path) as f:
            cases = yaml.safe_load(f)["cases"]

        total = len(cases)
        correct = wrong = deferred = 0
        wrong_cases = []

        for case in cases:
            r = clf.classify_sync(case["query"])
            expected = case["expected"]["module"]

            if r.module == "unknown":
                deferred += 1
            elif r.module == expected:
                correct += 1
            else:
                wrong += 1
                wrong_cases.append((case["id"], case["query"], expected, r.module, r.tier_used))

        handled = correct + wrong
        coverage = handled / total * 100
        accuracy = correct / handled * 100 if handled else 0

        print(f"\n{'='*60}")
        print(f"IntentClassifier SYNC -- Golden Set Report")
        print(f"{'='*60}")
        print(f"Total          : {total}")
        print(f"Correct        : {correct}")
        print(f"Wrong          : {wrong}")
        print(f"Deferred (LLM) : {deferred} ({deferred/total*100:.1f}%)")
        print(f"Coverage       : {coverage:.1f}%")
        print(f"Accuracy       : {accuracy:.1f}%")
        if wrong_cases:
            print(f"\nWrong ({len(wrong_cases)}):")
            for cid, q, exp, got, tier in wrong_cases:
                print(f"  [{cid}][{tier}] '{q[:45]}' exp={exp} got={got}")
        print(f"{'='*60}\n")

        assert coverage >= 55.0
        assert accuracy >= 85.0
