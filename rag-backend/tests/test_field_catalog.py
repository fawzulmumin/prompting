"""
Phase 23B (revised): Unit tests untuk FIELD_CATALOG, normalize_field_value,
dan distribusi pattern expansion — diperbarui dengan nilai aktual ES.
"""
import pytest
from app.domain.constants.field_catalog import (
    FIELD_CATALOG, DISTRIBUSI_FIELDS, normalize_field_value,
)


# ── normalize_field_value ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_normalize_akreditasi_alias_tt():
    assert normalize_field_value("akreditasi", "TT") == "Tidak Terakreditasi"


@pytest.mark.unit
def test_normalize_akreditasi_exact():
    assert normalize_field_value("akreditasi", "A") == "A"


@pytest.mark.unit
def test_normalize_akreditasi_case_insensitive():
    assert normalize_field_value("akreditasi", "b") == "B"


@pytest.mark.unit
def test_normalize_sumber_air_pdam():
    # Nilai aktual ES adalah "Ledeng/PAM" — catalog lama salah ("PAM/PDAM")
    assert normalize_field_value("sumber_air", "pdam") == "Ledeng/PAM"


@pytest.mark.unit
def test_normalize_sumber_air_sumur():
    assert normalize_field_value("sumber_air", "sumur") == "Sumur terlindungi"


@pytest.mark.unit
def test_normalize_unknown_field_passthrough():
    assert normalize_field_value("field_tidak_ada", "sembarang") == "sembarang"


@pytest.mark.unit
def test_normalize_unknown_value_passthrough():
    result = normalize_field_value("akreditasi", "NILAI_ANEH_XYZ")
    assert result == "NILAI_ANEH_XYZ"


@pytest.mark.unit
def test_normalize_status_kepemilikan_alias_pemda():
    assert normalize_field_value("status_kepemilikan", "pemda") == "Pemerintah Daerah"


# ── DISTRIBUSI_FIELDS ─────────────────────────────────────────────────────────

@pytest.mark.unit
def test_distribusi_fields_contains_expected():
    # Fields yang valid dan ada di ES aktual
    expected = {"akreditasi", "sumber_air", "waktu_penyelenggaraan", "status_kepemilikan"}
    assert expected.issubset(set(DISTRIBUSI_FIELDS))


@pytest.mark.unit
def test_distribusi_fields_not_include_non_distribusi():
    assert "status_sekolah" not in DISTRIBUSI_FIELDS
    assert "partisipasi_bos" not in DISTRIBUSI_FIELDS
    assert "bentuk_pendidikan" not in DISTRIBUSI_FIELDS


@pytest.mark.unit
def test_low_coverage_fields_not_in_distribusi():
    # sumber_listrik dan akses_internet coverage rendah — tidak boleh di DISTRIBUSI_FIELDS
    assert "sumber_listrik" not in DISTRIBUSI_FIELDS
    assert "akses_internet" not in DISTRIBUSI_FIELDS


# ── FIELD_CATALOG content ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_akreditasi_has_terakreditasi_value():
    # Nilai baru: "Terakreditasi" (ada ~507 dokumen di ES)
    assert "Terakreditasi" in FIELD_CATALOG["akreditasi"]["values"]
    assert FIELD_CATALOG["akreditasi"]["distribusi"] is True


@pytest.mark.unit
def test_status_kepemilikan_in_catalog():
    assert "status_kepemilikan" in FIELD_CATALOG
    assert "Yayasan" in FIELD_CATALOG["status_kepemilikan"]["values"]
    assert "Pemerintah Daerah" in FIELD_CATALOG["status_kepemilikan"]["values"]
    assert FIELD_CATALOG["status_kepemilikan"]["distribusi"] is True
    assert FIELD_CATALOG["status_kepemilikan"]["index"] == "profile"


@pytest.mark.unit
def test_false_positive_fields_not_in_catalog():
    # kondisi_bangunan dan kepemilikan_lahan tidak ada di ES → harus absen dari catalog
    assert "kondisi_bangunan" not in FIELD_CATALOG
    assert "kepemilikan_lahan" not in FIELD_CATALOG


@pytest.mark.unit
def test_sumber_air_values_are_actual_es_values():
    values = FIELD_CATALOG["sumber_air"]["values"]
    assert "Sumur terlindungi" in values
    assert "Ledeng/PAM" in values
    # Nilai lama catalog (salah) tidak boleh ada
    assert "PAM/PDAM" not in values
    assert "Sumur" not in values


# ── aggregation_handler pattern detection ─────────────────────────────────────

@pytest.mark.unit
def test_distribusi_pattern_status_kepemilikan_detected():
    from app.services.query_handlers.aggregation_handler import _infer_distribusi_field
    assert _infer_distribusi_field("distribusi status kepemilikan sekolah di Jawa Barat") == "status_kepemilikan"


@pytest.mark.unit
def test_distribusi_pattern_kepemilikan_milik_yayasan():
    from app.services.query_handlers.aggregation_handler import _infer_distribusi_field
    assert _infer_distribusi_field("berapa sekolah milik yayasan di Bandung") == "status_kepemilikan"


@pytest.mark.unit
def test_distribusi_pattern_partisipasi_bos():
    from app.services.query_handlers.aggregation_handler import _infer_distribusi_field
    assert _infer_distribusi_field("berapa sekolah peserta bos di Jakarta") == "partisipasi_bos"


@pytest.mark.unit
def test_distribusi_pattern_false_positive_removed():
    # kondisi_bangunan dan kepemilikan_lahan tidak ada di ES → pattern harus dihapus
    from app.services.query_handlers.aggregation_handler import _infer_distribusi_field
    assert _infer_distribusi_field("berapa sekolah dengan kondisi bangunan rusak berat") != "kondisi_bangunan"
    assert _infer_distribusi_field("distribusi kepemilikan lahan sekolah di Jawa Barat") != "kepemilikan_lahan"


@pytest.mark.unit
def test_distribusi_pattern_low_coverage_not_detected():
    # sumber_listrik (<0.04%) dan akses_internet (<0.01%) dihapus dari _DISTRIBUSI_PATTERNS
    from app.services.query_handlers.aggregation_handler import _infer_distribusi_field
    assert _infer_distribusi_field("distribusi sumber listrik SD di Papua Barat") != "sumber_listrik"
    assert _infer_distribusi_field("berapa sekolah dengan akses internet di Jawa Tengah") != "akses_internet"


# ── es_query_builder _normalize_status_sekolah ───────────────────────────────

@pytest.mark.unit
def test_normalize_status_sekolah_negeri():
    from app.services.core.es_query_builder import _normalize_status_sekolah
    assert set(_normalize_status_sekolah("NEGERI")) == {"NEGERI", "Negeri"}


@pytest.mark.unit
def test_normalize_status_sekolah_swasta_lowercase():
    from app.services.core.es_query_builder import _normalize_status_sekolah
    assert set(_normalize_status_sekolah("swasta")) == {"SWASTA", "Swasta"}


@pytest.mark.unit
def test_normalize_status_sekolah_no_luar_negeri():
    from app.services.core.es_query_builder import _normalize_status_sekolah
    # "Luar Negeri" adalah dirty data — tidak boleh masuk varian SWASTA
    swasta_variants = _normalize_status_sekolah("SWASTA")
    assert "Luar Negeri" not in swasta_variants
