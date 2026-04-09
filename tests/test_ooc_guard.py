# tests/test_ooc_guard.py
"""
Unit test untuk OOC guard layer:
1. intent_classifier.classify_intent()
2. domain_filter.is_education_query()

Jalankan: python -m pytest tests/test_ooc_guard.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.services.core.intent_classifier import classify_intent, IntentType
from app.services.core.cognitive_router import is_education_query

# ─────────────────────────────────────────────────────────────────────────────
# Query yang HARUS ditolak (OOC)
# ─────────────────────────────────────────────────────────────────────────────
OOC_QUERIES = [
    # Politik
    "siapa presiden sekarang?",
    "presiden indonesia siapa?",
    "presiden amerika sekarang",
    "siapa gubernur jawa barat?",
    "siapa bupati bogor?",
    "hasil pilpres 2024",
    "pemilu kapan?",
    # Ekonomi
    "harga bitcoin hari ini",
    "kurs dolar sekarang",
    "ihsg naik atau turun?",
    "harga emas hari ini",
    # Cuaca
    "cuaca hari ini di jakarta",
    "prakiraan cuaca besok",
    "gempa bumi tadi malam",
    # Hiburan
    "film terbaru 2024",
    "lagu terpopuler sekarang",
    # Kesehatan
    "gejala diabetes",
    "obat untuk demam",
    "rumah sakit terdekat",
    # Pertanian
    "harga sawit hari ini",
    "cara budidaya padi",
    # Frasa definitional non-sekolah
    "polandia dimana",
    "apa itu bitcoin",
    "sawit itu apa",
]

# ─────────────────────────────────────────────────────────────────────────────
# Query yang HARUS lolos (pendidikan)
# ─────────────────────────────────────────────────────────────────────────────
SCHOOL_QUERIES = [
    "berapa jumlah sekolah di jawa barat?",
    "daftar sekolah di kebayoran lama",
    "profil sman 1 bogor",
    "sd negeri di ciputat ada berapa?",
    "bandingkan sekolah di cibunar vs lumpang",
    "akreditasi smp negeri 1 lakbok",
    "jumlah siswa di kecamatan langensari",
    "sekolah swasta di kota banjar",
    "man kota banjar negeri atau swasta?",
    "npsn 20277088 itu sekolah apa?",
    "daftar mts negeri di kabupaten agam",
    "smk di parungpanjang",
    "ada berapa sd negeri di kecamatan lakbok?",
    "sekolah sma agam cendekia itu negeri atau swasta",
]


def test_ooc_intent_classifier():
    """Semua OOC query harus return OUT_OF_CONTEXT dari intent_classifier."""
    failed = []
    for q in OOC_QUERIES:
        result = classify_intent(q)
        if result != IntentType.OUT_OF_CONTEXT:
            failed.append((q, result))

    if failed:
        msg = "\n".join(f"  FAIL: {q!r} → {r}" for q, r in failed)
        raise AssertionError(f"\n{len(failed)} OOC query lolos intent_classifier:\n{msg}")

    print(f"✓ intent_classifier: semua {len(OOC_QUERIES)} OOC query ditolak")


def test_school_intent_classifier():
    """Semua school query TIDAK boleh return OUT_OF_CONTEXT dari intent_classifier."""
    failed = []
    for q in SCHOOL_QUERIES:
        result = classify_intent(q)
        if result == IntentType.OUT_OF_CONTEXT:
            failed.append((q, result))

    if failed:
        msg = "\n".join(f"  FAIL: {q!r} → {r}" for q, r in failed)
        raise AssertionError(
            f"\n{len(failed)} school query salah ditolak oleh intent_classifier:\n{msg}"
        )

    print(f"✓ intent_classifier: semua {len(SCHOOL_QUERIES)} school query lolos")


def test_ooc_domain_filter():
    """Semua OOC query harus return False dari is_education_query."""
    failed = []
    for q in OOC_QUERIES:
        result = is_education_query(q)
        if result is True:
            failed.append(q)

    if failed:
        msg = "\n".join(f"  FAIL: {q!r}" for q in failed)
        raise AssertionError(
            f"\n{len(failed)} OOC query lolos domain_filter:\n{msg}"
        )

    print(f"✓ domain_filter: semua {len(OOC_QUERIES)} OOC query ditolak")


def test_school_domain_filter():
    """Semua school query harus return True dari is_education_query."""
    failed = []
    for q in SCHOOL_QUERIES:
        result = is_education_query(q)
        if result is False:
            failed.append(q)

    if failed:
        msg = "\n".join(f"  FAIL: {q!r}" for q in failed)
        raise AssertionError(
            f"\n{len(failed)} school query salah ditolak domain_filter:\n{msg}"
        )

    print(f"✓ domain_filter: semua {len(SCHOOL_QUERIES)} school query lolos")


if __name__ == "__main__":
    print("=" * 60)
    print("OOC Guard Test Suite")
    print("=" * 60)

    tests = [
        test_ooc_intent_classifier,
        test_school_intent_classifier,
        test_ooc_domain_filter,
        test_school_domain_filter,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"✗ {test.__name__}:{e}")
            failed += 1

    print("=" * 60)
    print(f"Hasil: {passed} passed, {failed} failed")
    print("=" * 60)
