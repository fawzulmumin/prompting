"""
Golden Set Test Runner — Intent Classification
==============================================
Jalankan: pytest tests/golden_set/test_intent_classification.py -v
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
GOLDEN_SET_PATH = Path(__file__).parent / "golden_set_v1.yaml"


def load_golden_set() -> list[dict]:
    with open(GOLDEN_SET_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("cases", [])


def filter_cases(cases: list[dict], tag: str | None = None) -> list[dict]:
    if tag is None:
        return cases
    return [c for c in cases if tag in c.get("tags", [])]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def golden_cases() -> list[dict]:
    return load_golden_set()


@pytest.fixture(scope="session")
def all_tags(golden_cases) -> set[str]:
    tags: set[str] = set()
    for case in golden_cases:
        tags.update(case.get("tags", []))
    return tags


# ---------------------------------------------------------------------------
# Smoke test — pastikan file bisa dibaca
# ---------------------------------------------------------------------------
class TestGoldenSetStructure:
    def test_golden_set_loads(self, golden_cases):
        assert len(golden_cases) > 0, "Golden set harus punya minimal 1 case"

    def test_all_cases_have_required_fields(self, golden_cases):
        required = {"id", "query", "expected", "tags"}
        for case in golden_cases:
            missing = required - set(case.keys())
            assert not missing, f"Case {case.get('id', '?')} missing fields: {missing}"

    def test_all_ids_unique(self, golden_cases):
        ids = [c["id"] for c in golden_cases]
        assert len(ids) == len(set(ids)), "Duplicate IDs ditemukan"

    def test_all_expected_have_module(self, golden_cases):
        for case in golden_cases:
            assert "module" in case["expected"], (
                f"Case {case['id']}: expected.module harus ada"
            )

    def test_coverage_by_category(self, golden_cases):
        """Pastikan semua kategori utama ada di golden set."""
        modules = {c["expected"]["module"] for c in golden_cases}
        required_modules = {"aggregation", "list", "profile", "comparison", "out_of_context"}
        missing = required_modules - modules
        assert not missing, f"Module tidak tercakup di golden set: {missing}"


# ---------------------------------------------------------------------------
# Live tests — Tier 1+2 via IntentClassifier.classify_sync
# ---------------------------------------------------------------------------
# Known Tier 1+2 misclassifications accepted as Tier 3 territory
_XFAIL_T1T2: dict[str, str] = {
    "profile_005": "named private school + 'jumlah siswa' fires agg_count_kw in Tier 1",
}


class TestIntentClassificationBaseline:
    """
    Test menggunakan classify_intent_sync (Tier 1+2, no LLM) untuk speed.
    Cases yang Tier 1+2 defer atau tidak bisa handle -> xfail (expected).
    """

    @pytest.mark.parametrize("case", load_golden_set(), ids=[c["id"] for c in load_golden_set()])
    def test_intent_classification_sync(self, case: dict):
        from app.classification import classify_intent_sync

        result = classify_intent_sync(case["query"])
        expected_module = case["expected"]["module"]

        if result.module == "unknown":
            pytest.xfail(
                f"Deferred to LLM (expected) -- query={case['query']!r}"
            )

        # multi_intent is exclusively an LLM concept — Tier 1+2 can never produce it
        if expected_module == "multi_intent":
            pytest.xfail(
                f"multi_intent requires LLM -- Tier 1+2 classify as single module"
            )

        if case["id"] in _XFAIL_T1T2:
            pytest.xfail(
                f"Known Tier 1+2 limitation -- {_XFAIL_T1T2[case['id']]}"
            )

        assert result.module == expected_module, (
            f"Case {case['id']}: expected={expected_module} "
            f"got={result.module} tier={result.tier_used} conf={result.confidence:.2f}"
        )

    def test_oot_cases_never_misclassified_as_valid(self, golden_cases):
        """OOT cases TIDAK BOLEH return valid non-OOT module."""
        from app.classification import classify_intent_sync

        oot_cases = [c for c in golden_cases if c["expected"]["module"] == "out_of_context"]
        assert len(oot_cases) >= 3, "Butuh minimal 3 OOT cases"

        for case in oot_cases:
            result = classify_intent_sync(case["query"])
            if result.module != "unknown":
                assert result.module == "out_of_context", (
                    f"OOT case {case['id']} misclassified as {result.module!r}: "
                    f"query={case['query']!r}"
                )


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------
class TestGoldenSetReport:
    def test_generate_summary_report(self, golden_cases, capsys):
        """Generate summary statistik golden set ke stdout."""
        total = len(golden_cases)
        by_module: dict[str, int] = {}
        by_tag: dict[str, int] = {}

        for case in golden_cases:
            mod = case["expected"]["module"]
            by_module[mod] = by_module.get(mod, 0) + 1
            for tag in case.get("tags", []):
                by_tag[tag] = by_tag.get(tag, 0) + 1

        print(f"\n{'='*60}")
        print(f"GOLDEN SET SUMMARY — v1.0")
        print(f"{'='*60}")
        print(f"Total cases: {total}")
        print(f"\nDistribusi per modul:")
        for mod, count in sorted(by_module.items(), key=lambda x: -x[1]):
            pct = count / total * 100
            bar = "█" * int(pct / 5)
            print(f"  {mod:<20} {count:>3} ({pct:5.1f}%) {bar}")
        print(f"\nTop tags:")
        for tag, count in sorted(by_tag.items(), key=lambda x: -x[1])[:10]:
            print(f"  {tag:<25} {count}")
        print(f"{'='*60}\n")

        # Assert minimal coverage
        assert total >= 40, f"Golden set harus punya minimal 40 cases, saat ini {total}"
        assert by_module.get("out_of_context", 0) >= 3, "Butuh minimal 3 OOT cases"
