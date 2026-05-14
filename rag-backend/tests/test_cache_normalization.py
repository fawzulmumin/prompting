"""
Test Cache Key Normalization
Jalankan: pytest tests/test_cache_normalization.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from app.utils.cache import _normalize_cache_key


class TestCurrentBehavior:
    """Capture baseline behavior sebelum improvement."""

    def test_basic_lowercase(self):
        assert _normalize_cache_key("Bandung") == _normalize_cache_key("bandung")

    def test_whitespace_normalized(self):
        assert _normalize_cache_key("SD  di  bandung") == _normalize_cache_key("SD di bandung")

    def test_punctuation_stripped(self):
        assert _normalize_cache_key("kab. bandung") == _normalize_cache_key("kab bandung")


class TestImprovedBehavior:
    """Target behavior setelah improvement — ini yang harus PASS setelah perubahan."""

    # --- Alias normalization ---
    def test_alias_tangsel(self):
        a = _normalize_cache_key("berapa SMK di tangsel")
        b = _normalize_cache_key("berapa SMK di tangerang selatan")
        assert a == b, f"alias 'tangsel' harus == 'tangerang selatan'\n  a={a}\n  b={b}"

    def test_alias_jaksel(self):
        a = _normalize_cache_key("sekolah di jaksel")
        b = _normalize_cache_key("sekolah di jakarta selatan")
        assert a == b

    def test_alias_dki(self):
        a = _normalize_cache_key("SD di DKI")
        b = _normalize_cache_key("SD di dki jakarta")
        assert a == b

    def test_alias_jabar(self):
        a = _normalize_cache_key("SMP di jabar")
        b = _normalize_cache_key("SMP di jawa barat")
        assert a == b

    def test_alias_jatim(self):
        a = _normalize_cache_key("SMA di jatim")
        b = _normalize_cache_key("SMA di jawa timur")
        assert a == b

    # --- Stopword / filler removal ---
    def test_stopword_berapa_vs_jumlah(self):
        a = _normalize_cache_key("berapa SD di bandung")
        b = _normalize_cache_key("jumlah SD di bandung")
        assert a == b, f"'berapa' dan 'jumlah' harus normalize sama\n  a={a}\n  b={b}"

    def test_stopword_ada_berapa(self):
        a = _normalize_cache_key("ada berapa SMP di bekasi")
        b = _normalize_cache_key("berapa SMP di bekasi")
        assert a == b

    def test_stopword_tampilkan(self):
        a = _normalize_cache_key("tampilkan sekolah di surabaya")
        b = _normalize_cache_key("sekolah di surabaya")
        assert a == b

    def test_stopword_cari(self):
        a = _normalize_cache_key("cari SMK di jakarta")
        b = _normalize_cache_key("SMK di jakarta")
        assert a == b

    def test_stopword_tolong(self):
        a = _normalize_cache_key("tolong tampilkan SD di medan")
        b = _normalize_cache_key("SD di medan")
        assert a == b

    # --- Location prefix normalization ---
    def test_prefix_kab(self):
        a = _normalize_cache_key("sekolah di kab bogor")
        b = _normalize_cache_key("sekolah di kabupaten bogor")
        assert a == b

    def test_prefix_kota(self):
        a = _normalize_cache_key("SMP di kota bandung")
        b = _normalize_cache_key("SMP di bandung")
        assert a == b

    def test_prefix_kec(self):
        a = _normalize_cache_key("SD di kec cibinong")
        b = _normalize_cache_key("SD di kecamatan cibinong")
        assert a == b

    def test_prefix_prov(self):
        a = _normalize_cache_key("SMK di prov jawa barat")
        b = _normalize_cache_key("SMK di provinsi jawa barat")
        assert a == b

    # --- Token sort (word order invariant) ---
    def test_token_order_invariant(self):
        a = _normalize_cache_key("bandung SD jumlah")
        b = _normalize_cache_key("jumlah SD bandung")
        # Setelah stopword removal dan sort, harus sama
        assert a == b

    # --- Jenjang normalization ---
    def test_jenjang_sekolah_dasar(self):
        a = _normalize_cache_key("sekolah dasar di bogor")
        b = _normalize_cache_key("SD di bogor")
        assert a == b

    def test_jenjang_sekolah_menengah_pertama(self):
        a = _normalize_cache_key("sekolah menengah pertama di bekasi")
        b = _normalize_cache_key("SMP di bekasi")
        assert a == b

    def test_jenjang_sekolah_menengah_atas(self):
        a = _normalize_cache_key("sekolah menengah atas di surabaya")
        b = _normalize_cache_key("SMA di surabaya")
        assert a == b

    def test_jenjang_madrasah_ibtidaiyah(self):
        a = _normalize_cache_key("madrasah ibtidaiyah di NTT")
        b = _normalize_cache_key("MI di nusa tenggara timur")
        assert a == b

    def test_jenjang_sekolah_luar_biasa(self):
        a = _normalize_cache_key("sekolah luar biasa di sulsel")
        b = _normalize_cache_key("SLB di sulawesi selatan")
        assert a == b

    # --- Typo tolerance (soft) ---
    def test_typo_berapa(self):
        a = _normalize_cache_key("brapa SD di bandung")
        b = _normalize_cache_key("berapa SD di bandung")
        # Setelah stopword removal, keduanya jadi "sd bandung"
        assert a == b

    def test_typo_provinsi(self):
        a = _normalize_cache_key("propinsi jawa barat")
        b = _normalize_cache_key("provinsi jawa barat")
        assert a == b

    # --- Combined ---
    def test_combined_alias_stopword_prefix(self):
        a = _normalize_cache_key("ada berapa SMK di kab tangsel")
        b = _normalize_cache_key("jumlah SMK di kabupaten tangerang selatan")
        assert a == b

    def test_combined_typo_alias(self):
        a = _normalize_cache_key("brapa SMP di jaksel")
        b = _normalize_cache_key("berapa SMP di jakarta selatan")
        assert a == b
