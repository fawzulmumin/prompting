"""Tests for list_location_detector utility functions (Phase 24A-1)."""
import pytest


@pytest.mark.unit
def test_strip_numeric_condition_tokens():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens

    cases = [
        ("guru kurang dari 5 di Sulawesi", "Sulawesi"),
        ("SMA dengan siswa lebih dari 300 di Bali", "Bali"),
        ("daftar SMA yang punya minimal 10 komputer di pulau jawa", "jawa"),
        ("sekolah di Jakarta", "Jakarta"),
        ("SD di Jawa Barat dengan guru PNS lebih dari 10", "Barat"),
    ]
    for raw, expected_contains in cases:
        result = _strip_numeric_condition_tokens(raw)
        wilayah = expected_contains.split()[-1]
        assert wilayah.lower() in result.lower(), (
            f"Wilayah '{wilayah}' hilang dari '{result}' (input: '{raw}')"
        )
        assert "kurang dari" not in result.lower()
        assert "lebih dari" not in result.lower()
        assert "minimal" not in result.lower() or "minimal" not in raw.lower()


@pytest.mark.unit
def test_strip_preserves_jenjang():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens

    result = _strip_numeric_condition_tokens(
        "daftar SMA yang punya lebih dari 5 guru PNS di Bali"
    )
    assert "SMA" in result
    assert "Bali" in result
    assert "lebih dari" not in result.lower()


@pytest.mark.unit
def test_strip_no_condition_unchanged():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens

    query = "sekolah di Jakarta"
    result = _strip_numeric_condition_tokens(query)
    assert "jakarta" in result.lower()
    assert "sekolah" in result.lower()


@pytest.mark.unit
def test_strip_between_condition():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens

    result = _strip_numeric_condition_tokens(
        "sekolah antara 100 sampai 500 siswa di Kalimantan Timur"
    )
    assert "kalimantan" in result.lower()
    assert "timur" in result.lower()
    assert "antara" not in result.lower()
    assert "sampai" not in result.lower()


@pytest.mark.unit
def test_strip_dengan_guru():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens

    result = _strip_numeric_condition_tokens(
        "daftar smp negeri di sulawesi dengan guru kurang dari 5 orang"
    )
    assert "guru" not in result.lower()
    assert "sulawesi" in result.lower()
    assert "kurang dari" not in result.lower()


@pytest.mark.unit
def test_strip_dengan_siswa():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens

    result = _strip_numeric_condition_tokens(
        "SD di Jawa Barat dengan siswa lebih dari 300"
    )
    assert "siswa" not in result.lower()
    assert "jawa" in result.lower()
    assert "barat" in result.lower()
    assert "lebih dari" not in result.lower()


@pytest.mark.unit
def test_strip_yang_punya_komputer():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens

    result = _strip_numeric_condition_tokens(
        "daftar SMA yang punya minimal 10 komputer di pulau jawa"
    )
    assert "komputer" not in result.lower()
    assert "sma" in result.lower()
    assert "jawa" in result.lower()


@pytest.mark.unit
def test_pulau_jawa_resolves_to_provinsi():
    from app.services.core.list_location_detector import _resolve_pulau_keyword

    result = _resolve_pulau_keyword("pulau jawa")
    assert result is not None
    pulau_name, provinces = result
    assert pulau_name == "jawa"
    assert len(provinces) == 6
    assert "prov. jawa barat" in provinces


@pytest.mark.unit
def test_sulawesi_alias():
    from app.services.core.list_location_detector import _resolve_pulau_keyword

    result = _resolve_pulau_keyword("sulawesi")
    assert result is not None
    _, provinces = result
    assert len(provinces) == 6


@pytest.mark.unit
def test_strip_wilayah_perbatasan():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens
    result = _strip_numeric_condition_tokens("daftar smp di wilayah perbatasan kalimantan")
    assert "perbatasan" not in result.lower()
    assert "kalimantan" in result.lower()


@pytest.mark.unit
def test_strip_wilayah_terpencil():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens
    result = _strip_numeric_condition_tokens("sekolah terpencil di papua barat")
    assert "terpencil" not in result.lower()
    assert "papua" in result.lower()


@pytest.mark.unit
def test_strip_3t():
    from app.services.core.list_location_detector import _strip_numeric_condition_tokens
    result = _strip_numeric_condition_tokens("SD di wilayah 3T Sulawesi")
    assert "3t" not in result.lower()
    assert "sulawesi" in result.lower()
