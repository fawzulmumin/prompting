"""
Test PatternClassifier — Tier 1
Jalankan: pytest tests/test_tier1_pattern.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app.classification.tier1_pattern import PatternClassifier, classify_pattern, CONFIDENCE_THRESHOLD


@pytest.fixture(scope="module")
def clf():
    return PatternClassifier()


class TestOOT:
    def test_presiden(self, clf):
        r = clf.classify("siapa presiden Indonesia sekarang")
        assert r.module == "out_of_context"
        assert r.confidence >= CONFIDENCE_THRESHOLD

    def test_harga_laptop(self, clf):
        r = clf.classify("harga laptop terbaru 2025")
        assert r.module == "out_of_context"

    def test_puisi(self, clf):
        r = clf.classify("tolong buatkan puisi tentang pendidikan")
        assert r.module == "out_of_context"

    def test_cuaca(self, clf):
        r = clf.classify("cuaca hari ini di jakarta")
        assert r.module == "out_of_context"


class TestProfile:
    def test_npsn_explicit(self, clf):
        r = clf.classify("info sekolah NPSN 20217725")
        assert r.module == "profile"
        assert r.confidence >= CONFIDENCE_THRESHOLD

    def test_npsn_number_only(self, clf):
        r = clf.classify("20217725")
        assert r.module == "profile"

    def test_sman_number(self, clf):
        r = clf.classify("profil SMAN 1 Bandung")
        assert r.module == "profile"

    def test_sdn_number(self, clf):
        r = clf.classify("SDN 5 Bogor")
        assert r.module == "profile"


class TestGeo:
    def test_terdekat(self, clf):
        r = clf.classify("sekolah terdekat dari monas")
        assert r.module == "geo"
        assert r.confidence >= CONFIDENCE_THRESHOLD

    def test_koordinat(self, clf):
        r = clf.classify("SMA terdekat dari koordinat -6.2088,106.8456")
        assert r.module == "geo"

    def test_dekat_stasiun(self, clf):
        r = clf.classify("SMP negeri dekat stasiun depok baru")
        assert r.module == "geo"


class TestComparison:
    def test_vs(self, clf):
        r = clf.classify("SMA di Bandung vs Bekasi")
        assert r.module == "comparison"
        assert r.confidence >= CONFIDENCE_THRESHOLD

    def test_bandingkan(self, clf):
        r = clf.classify("bandingkan jumlah SMK di Jawa Barat dan Jawa Timur")
        assert r.module == "comparison"

    def test_mana_lebih(self, clf):
        r = clf.classify("mana yang lebih banyak SMP, Jakarta atau Surabaya")
        assert r.module == "comparison"


class TestAggregation:
    def test_berapa_sd(self, clf):
        r = clf.classify("berapa SD di jawa barat")
        assert r.module == "aggregation"
        assert r.confidence >= CONFIDENCE_THRESHOLD

    def test_jumlah(self, clf):
        r = clf.classify("jumlah SMP negeri di bandung")
        assert r.module == "aggregation"

    def test_total(self, clf):
        r = clf.classify("total sekolah akreditasi A di surabaya")
        assert r.module == "aggregation"

    def test_brapa_typo(self, clf):
        r = clf.classify("brapa smk di tangsel")
        assert r.module == "aggregation"

    def test_ranking(self, clf):
        r = clf.classify("ranking SMK terbanyak per provinsi")
        assert r.module == "aggregation"


class TestList:
    def test_daftar(self, clf):
        r = clf.classify("daftar SMA negeri di jakarta selatan")
        assert r.module == "list"
        assert r.confidence >= CONFIDENCE_THRESHOLD

    def test_tampilkan(self, clf):
        r = clf.classify("tampilkan sekolah akreditasi A di kota bandung")
        assert r.module == "list"

    def test_cari(self, clf):
        r = clf.classify("cari SMK swasta di bekasi yang akreditasi A")
        assert r.module == "list"

    def test_ada_sekolah_apa(self, clf):
        r = clf.classify("ada sekolah apa saja di desa cibunar parungpanjang")
        assert r.module == "list"


class TestAmbiguous:
    """Query ambigu harus return None (serahkan ke tier berikutnya)."""

    def test_minimal_query_ambiguous(self, clf):
        r = clf.classify("sekolah jakarta")
        # Boleh None ATAU list/aggregation — yang penting tidak out_of_context
        assert r.module != "out_of_context"

    def test_no_match_returns_none(self, clf):
        r = clf.classify("xyz abc def ghi")
        assert r.module is None
        assert r.confidence == 0.0


class TestPerformance:
    def test_latency_under_5ms(self, clf):
        import time
        queries = [
            "berapa SD di jawa barat",
            "daftar SMA negeri di jakarta selatan",
            "bandingkan SMK di Bandung vs Bekasi",
            "sekolah terdekat dari monas",
            "siapa presiden Indonesia",
        ]
        times = []
        for q in queries:
            t0 = time.perf_counter()
            clf.classify(q)
            times.append((time.perf_counter() - t0) * 1000)

        avg_ms = sum(times) / len(times)
        max_ms = max(times)
        print(f"\nAvg: {avg_ms:.2f}ms, Max: {max_ms:.2f}ms")
        assert avg_ms < 5.0, f"Average latency {avg_ms:.2f}ms melebihi target 5ms"
        assert max_ms < 20.0, f"Max latency {max_ms:.2f}ms terlalu tinggi"

    def test_explain_debug(self, clf):
        output = clf.explain("berapa SD di bandung")
        assert "Module" in output
        assert "aggregation" in output


class TestGoldenSetCoverage:
    """Cek berapa % golden set bisa di-handle Tier 1 (confidence >= threshold)."""

    def test_golden_set_coverage_report(self, clf):
        import yaml
        from pathlib import Path

        golden_path = Path(__file__).parent / "golden_set" / "golden_set_v1.yaml"
        if not golden_path.exists():
            pytest.skip("Golden set tidak ditemukan")

        with open(golden_path) as f:
            cases = yaml.safe_load(f)["cases"]

        total = len(cases)
        handled = 0      # tier1 bisa handle (ada module)
        correct = 0      # tier1 handle DAN benar
        wrong = 0        # tier1 handle TAPI salah
        deferred = 0     # tier1 defer ke tier berikutnya (module=None)

        wrong_cases = []

        for case in cases:
            result = clf.classify(case["query"])
            expected_module = case["expected"]["module"]

            if result.module is None:
                deferred += 1
            else:
                handled += 1
                if result.module == expected_module:
                    correct += 1
                else:
                    wrong += 1
                    wrong_cases.append({
                        "id": case["id"],
                        "query": case["query"],
                        "expected": expected_module,
                        "got": result.module,
                        "conf": result.confidence,
                        "pattern": result.matched_pattern,
                    })

        coverage_pct = handled / total * 100
        accuracy_pct = correct / handled * 100 if handled > 0 else 0
        precision_pct = correct / total * 100

        print(f"\n{'='*60}")
        print(f"TIER 1 PATTERN CLASSIFIER - Golden Set Report")
        print(f"{'='*60}")
        print(f"Total cases    : {total}")
        print(f"Handled (conf>={CONFIDENCE_THRESHOLD:.0%}): {handled} ({coverage_pct:.1f}%)")
        print(f"  Correct      : {correct}")
        print(f"  Wrong        : {wrong}")
        print(f"Deferred       : {deferred} ({deferred/total*100:.1f}%)")
        print(f"Coverage       : {coverage_pct:.1f}%  (target ≥40%)")
        print(f"Accuracy       : {accuracy_pct:.1f}%  (target ≥90% dari yang di-handle)")
        print(f"Precision      : {precision_pct:.1f}%  (correct/total)")

        if wrong_cases:
            print(f"\nWrong classifications ({len(wrong_cases)}):")
            for w in wrong_cases:
                print(f"  [{w['id']}] '{w['query'][:50]}'")
                print(f"    expected={w['expected']} got={w['got']} conf={w['conf']:.2f} pattern={w['pattern']}")
        print(f"{'='*60}\n")

        # Assertions
        assert coverage_pct >= 40.0, f"Coverage {coverage_pct:.1f}% di bawah target 40%"
        assert accuracy_pct >= 85.0, f"Accuracy {accuracy_pct:.1f}% di bawah target 85%"
        # Wrong tidak boleh lebih dari 10 cases (threshold dinaikkan seiring golden set 150+ cases)
        assert wrong <= 10, f"Terlalu banyak wrong classification: {wrong}"
