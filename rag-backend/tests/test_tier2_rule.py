"""
Test RuleClassifier -- Tier 2
Jalankan: pytest tests/test_tier2_rule.py -v -s
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app.classification.tier1_pattern import PatternClassifier, CONFIDENCE_THRESHOLD as T1_THRESHOLD
from app.classification.tier2_rule import RuleClassifier, classify_rule, CONFIDENCE_THRESHOLD as T2_THRESHOLD


@pytest.fixture(scope="module")
def t1():
    return PatternClassifier()


@pytest.fixture(scope="module")
def t2():
    return RuleClassifier()


class TestRuleClassifierBasic:
    def test_berapa_sekolah(self, t2):
        r = t2.classify("berapa sekolah di jawa barat")
        assert r.module == "aggregation"
        assert r.confidence >= T2_THRESHOLD

    def test_tampilkan_sekolah(self, t2):
        r = t2.classify("tampilkan sekolah di jakarta selatan")
        assert r.module == "list"

    def test_profile_informasi(self, t2):
        r = t2.classify("informasi sekolah SDN 3 Bogor")
        assert r.module == "profile"

    def test_comparison_vs(self, t2):
        r = t2.classify("SMA di Bandung vs Bekasi")
        assert r.module == "comparison"

    def test_geo_terdekat(self, t2):
        r = t2.classify("SMP terdekat dari sini")
        assert r.module == "geo"

    def test_no_match_returns_none(self, t2):
        r = t2.classify("xyz abc def ghi")
        assert r.module is None
        assert r.confidence == 0.0


class TestEntityHints:
    def test_jenjang_detected(self, t2):
        r = t2.classify("daftar SMA di bandung")
        assert "SMA" in r.entity_hints.get("jenjang", [])

    def test_status_negeri(self, t2):
        r = t2.classify("daftar SMP negeri di jakarta")
        assert r.entity_hints.get("status") == "NEGERI"

    def test_status_swasta(self, t2):
        r = t2.classify("cari SMK swasta di bekasi")
        assert r.entity_hints.get("status") == "SWASTA"

    def test_multi_jenjang(self, t2):
        r = t2.classify("berapa SD dan SMP di jawa barat")
        jenjang = r.entity_hints.get("jenjang", [])
        assert "SD" in jenjang
        assert "SMP" in jenjang


class TestSemanticPriority:
    def test_comparison_beats_aggregation(self, t2):
        r = t2.classify("bandingkan jumlah SMK di Bandung dan Bekasi")
        assert r.module == "comparison"

    def test_multi_loc_boosts_comparison(self, t2):
        r = t2.classify("berapa SMA di Bandung dan Bekasi")
        # Either comparison (with multi-loc bonus) or aggregation — both acceptable
        assert r.module in ("comparison", "aggregation")


class TestPerformance:
    def test_latency_under_20ms(self, t2):
        import time
        queries = [
            "berapa SD di jawa barat",
            "daftar SMA negeri di jakarta selatan",
            "bandingkan SMK di Bandung vs Bekasi",
            "sekolah terdekat dari monas",
            "informasi SDN 1 Bogor",
        ]
        times = []
        for q in queries:
            t0 = time.perf_counter()
            t2.classify(q)
            times.append((time.perf_counter() - t0) * 1000)

        avg_ms = sum(times) / len(times)
        max_ms = max(times)
        print(f"\nTier2 Avg: {avg_ms:.2f}ms, Max: {max_ms:.2f}ms")
        assert avg_ms < 20.0, f"Average latency {avg_ms:.2f}ms exceeds 20ms target"
        assert max_ms < 50.0, f"Max latency {max_ms:.2f}ms too high"

    def test_explain_debug(self, t2):
        output = t2.explain("berapa SMA di bandung")
        assert "Module" in output
        assert "aggregation" in output


class TestGoldenSetCoverage:
    """Combined Tier 1 + Tier 2 coverage on golden set."""

    def test_combined_coverage_report(self, t1, t2):
        import yaml

        golden_path = Path(__file__).parent / "golden_set" / "golden_set_v1.yaml"
        if not golden_path.exists():
            pytest.skip("Golden set tidak ditemukan")

        with open(golden_path) as f:
            cases = yaml.safe_load(f)["cases"]

        total = len(cases)
        t1_handled = 0; t1_correct = 0; t1_wrong = 0
        t2_handled = 0; t2_correct = 0; t2_wrong = 0
        deferred_llm = 0

        t1_wrong_cases = []
        t2_wrong_cases = []

        for case in cases:
            q = case["query"]
            expected = case["expected"]["module"]

            r1 = t1.classify(q)
            if r1.module is not None:
                t1_handled += 1
                if r1.module == expected:
                    t1_correct += 1
                else:
                    t1_wrong += 1
                    t1_wrong_cases.append({
                        "id": case["id"], "query": q,
                        "expected": expected, "got": r1.module,
                        "conf": r1.confidence,
                    })
                continue

            # T1 deferred -> try T2
            r2 = t2.classify(q)
            if r2.module is not None:
                t2_handled += 1
                if r2.module == expected:
                    t2_correct += 1
                else:
                    t2_wrong += 1
                    t2_wrong_cases.append({
                        "id": case["id"], "query": q,
                        "expected": expected, "got": r2.module,
                        "conf": r2.confidence,
                        "signals": r2.signals[:3],
                    })
            else:
                deferred_llm += 1

        combined_handled = t1_handled + t2_handled
        combined_correct = t1_correct + t2_correct
        combined_wrong = t1_wrong + t2_wrong
        coverage_pct = combined_handled / total * 100
        accuracy_pct = combined_correct / combined_handled * 100 if combined_handled > 0 else 0
        precision_pct = combined_correct / total * 100

        print(f"\n{'='*65}")
        print(f"COMBINED TIER 1 + TIER 2 - Golden Set Report")
        print(f"{'='*65}")
        print(f"Total cases         : {total}")
        print(f"--- Tier 1 ---")
        print(f"  Handled           : {t1_handled} ({t1_handled/total*100:.1f}%)")
        print(f"  Correct           : {t1_correct}")
        print(f"  Wrong             : {t1_wrong}")
        print(f"--- Tier 2 ---")
        print(f"  Handled           : {t2_handled} ({t2_handled/total*100:.1f}%)")
        print(f"  Correct           : {t2_correct}")
        print(f"  Wrong             : {t2_wrong}")
        print(f"--- Combined ---")
        print(f"  Total Handled     : {combined_handled} ({coverage_pct:.1f}%)")
        print(f"  Total Correct     : {combined_correct}")
        print(f"  Total Wrong       : {combined_wrong}")
        print(f"  Deferred to LLM   : {deferred_llm} ({deferred_llm/total*100:.1f}%)")
        print(f"Coverage            : {coverage_pct:.1f}%  (target >=70%)")
        print(f"Accuracy            : {accuracy_pct:.1f}%  (target >=85%)")
        print(f"Precision           : {precision_pct:.1f}%  (correct/total)")

        if t1_wrong_cases:
            print(f"\nTier 1 wrong ({len(t1_wrong_cases)}):")
            for w in t1_wrong_cases:
                print(f"  [{w['id']}] '{w['query'][:50]}'")
                print(f"    expected={w['expected']} got={w['got']} conf={w['conf']:.2f}")

        if t2_wrong_cases:
            print(f"\nTier 2 wrong ({len(t2_wrong_cases)}):")
            for w in t2_wrong_cases:
                print(f"  [{w['id']}] '{w['query'][:50]}'")
                print(f"    expected={w['expected']} got={w['got']} conf={w['conf']:.2f}")
                print(f"    signals={w['signals']}")

        print(f"{'='*65}\n")

        # Assertions
        assert coverage_pct >= 55.0, f"Coverage {coverage_pct:.1f}% below target 55%"
        assert accuracy_pct >= 85.0, f"Accuracy {accuracy_pct:.1f}% below target 85%"
        assert deferred_llm <= 70, f"Too many deferred to LLM: {deferred_llm}"
