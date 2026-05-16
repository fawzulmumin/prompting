"""
Regression tests for Priority 1: Dependency-Aware Execution.

Covers:
  - _keyword_fallback() detecting ranking+list pattern → sets dependencies
  - _build_injected_intent() injects parent result without mutating original
  - _keyword_fallback() returns empty dependencies for single-intent queries
"""

import pytest

from app.services.core.cognitive_router import (
    CognitiveIntent,
    WilayahEntities,
    _keyword_fallback,
)
from app.services.core.multi_intent_orchestrator import _build_injected_intent
from app.services.query_handlers.comparison_handler import _extract_wilayah_from_intent
from app.services.query_handlers.list_school_handler import _inject_intent_wilayah


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 1: _keyword_fallback dependency detection
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_dependency_detection_keyword():
    """
    Protects against: ranking+list query not setting dependencies, causing list
    to run before aggregation completes (no provinsi injected → wrong results).
    """
    intent = _keyword_fallback(
        "provinsi dengan SMK terbanyak tampilkan 10 sekolahnya"
    )

    assert intent.dependencies != {}, "dependencies should be set for ranking+list query"
    assert "list" in intent.dependencies, "list module should be the dependent module"
    assert intent.dependencies["list"]["depends_on"] == "aggregation"
    assert "inject_from_result" in intent.dependencies["list"]
    assert intent.dependencies["list"]["inject_from_result"].get("provinsi") == "top_location"


@pytest.mark.unit
def test_no_dependency_single_intent():
    """
    Protects against: single-intent query accidentally gaining a dependency entry,
    which would force unnecessary 2-tier execution.
    """
    intent = _keyword_fallback("berapa SD di Jakarta")

    assert intent.dependencies == {}, (
        f"single-intent query must have empty dependencies, got {intent.dependencies}"
    )


@pytest.mark.unit
def test_dependency_detection_sets_aggregation_as_primary():
    """
    Protects against: ranking+list query routing to module='list' instead of
    'aggregation', which would skip the ranking step entirely.
    """
    intent = _keyword_fallback(
        "provinsi dengan SMK terbanyak tampilkan 10 sekolahnya"
    )

    assert intent.module == "aggregation", (
        f"primary module must be 'aggregation' for ranking query, got '{intent.module}'"
    )
    assert "list" in intent.secondary, "list must be in secondary for the dependent tier"


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 2: _build_injected_intent injection + immutability
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_orchestrator_build_injected_intent():
    """
    Protects against: injecting parent result mutating original intent (side effect
    would corrupt all downstream modules that share the same intent object).
    """
    original = CognitiveIntent(module="list", wilayah=WilayahEntities(provinsi=None))

    result = _build_injected_intent(
        original,
        {"top_location": "Jawa Barat"},
        {"provinsi": "top_location"},
    )

    # Injection must work
    assert result.wilayah.provinsi == "jawa barat", (
        f"Expected 'jawa barat', got '{result.wilayah.provinsi}'"
    )
    # Scope updated to match injected field
    assert result.scope == "provinsi"

    # Original must NOT be mutated
    assert original.wilayah.provinsi is None, (
        "original intent.wilayah.provinsi must remain None — _build_injected_intent mutated it!"
    )


@pytest.mark.unit
def test_build_injected_intent_missing_key_is_noop():
    """
    Protects against: injection crashing or corrupting intent when parent result
    does not contain the expected key (e.g., aggregation returned no top_location).
    """
    original = CognitiveIntent(module="list", wilayah=WilayahEntities(provinsi=None))

    result = _build_injected_intent(
        original,
        {},  # parent returned nothing
        {"provinsi": "top_location"},
    )

    # No injection happened — wilayah should remain unchanged
    assert result.wilayah.provinsi is None
    # Original untouched
    assert original.wilayah.provinsi is None


@pytest.mark.unit
def test_build_injected_intent_lowercases_value():
    """
    Protects against: injected value remaining mixed-case, causing ES term-level
    queries to fail (ES keyword fields are normalised to lowercase).
    """
    original = CognitiveIntent(module="list", wilayah=WilayahEntities(provinsi=None))

    result = _build_injected_intent(
        original,
        {"top_location": "JAWA TENGAH"},
        {"provinsi": "top_location"},
    )

    assert result.wilayah.provinsi == "jawa tengah"


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 3: comparison_handler._extract_wilayah_from_intent
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_comparison_handler_uses_intent_wilayah():
    """
    Protects against: comparison handler calling string-splitting resolver even when
    classifier already parsed both locations — causing double-resolution latency and
    potential mis-match on ambiguous names.
    """
    intent = CognitiveIntent(
        module="comparison",
        wilayah=WilayahEntities(kabupaten="bandung"),
        wilayah_bandingkan="bogor",
    )

    result = _extract_wilayah_from_intent(intent)

    assert result["wilayah_1"] == "bandung", (
        f"Expected 'bandung', got '{result['wilayah_1']}'"
    )
    assert result["wilayah_2"] == "bogor", (
        f"Expected 'bogor', got '{result['wilayah_2']}'"
    )
    assert result["scope"] == "kabupaten", (
        f"Expected scope='kabupaten', got '{result['scope']}'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 4: list_school_handler._inject_intent_wilayah
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_list_inject_intent_wilayah_fills_empty_parsed():
    """
    Protects against: list handler running full string-parsing when classifier already
    extracted wilayah — causing empty-wilayah → clarification response instead of results.
    """
    intent = CognitiveIntent(
        module="list",
        wilayah=WilayahEntities(provinsi="jawa barat"),
    )
    intent.scope = "provinsi"

    parsed = {"wilayah": None, "wilayah_level": None}
    _inject_intent_wilayah(parsed, intent)

    assert parsed["wilayah"] == "jawa barat", (
        f"Expected 'jawa barat', got '{parsed['wilayah']}'"
    )
    assert parsed["wilayah_level"] == "provinsi", (
        f"Expected 'provinsi', got '{parsed['wilayah_level']}'"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 5: cache helpers (_log, _normalize_cache_key)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_log_emits_valid_json():
    """
    Protects against: _log() emitting non-JSON output that breaks log aggregators
    expecting structured JSON lines (e.g., Datadog, CloudWatch Logs Insights).
    """
    import json
    import logging
    from io import StringIO
    from app.utils.cache import _log

    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))

    test_logger = logging.getLogger("test_log_helper")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)
    test_logger.propagate = False

    _log(test_logger, "info", "test_event", key="abc", ttl=300)

    output = stream.getvalue().strip()
    data = json.loads(output)

    assert data["event"] == "test_event"
    assert isinstance(data["ts"], float)
    assert data["key"] == "abc"


@pytest.mark.unit
def test_normalize_cache_key_lowercases_and_strips():
    """
    Protects against: minor query variations (whitespace, punctuation, casing)
    causing cache misses even when semantic content is identical.
    """
    from app.utils.cache import _normalize_cache_key

    # "berapa" is a stopword → stripped; punctuation/whitespace/casing → cleaned
    assert _normalize_cache_key("  Berapa SMK  ") == "smk"
    assert _normalize_cache_key("berapa,SMK!") == "smk"

    md5_like = "a" * 32  # 32 hex chars — must pass through unchanged
    assert _normalize_cache_key(md5_like) == md5_like


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 6: deepseek rate limit & circuit breaker
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_rate_limit_check_allows_under_limit():
    """
    Protects against: rate limiter blocking valid requests or failing to increment
    the counter when the current count is below the configured maximum.
    """
    from unittest.mock import AsyncMock, patch
    from app.providers.deepseek import _check_rate_limit

    with patch("app.providers.deepseek.get_counter", new_callable=AsyncMock) as mock_get, \
         patch("app.providers.deepseek.increment_counter", new_callable=AsyncMock) as mock_inc:
        mock_get.return_value = 10  # well under limit of 50
        mock_inc.return_value = 11

        result = await _check_rate_limit()

        assert result is True
        mock_inc.assert_called_once()


@pytest.mark.unit
async def test_rate_limit_check_blocks_over_limit():
    """
    Protects against: rate limiter allowing requests through and still incrementing
    the counter when the limit is already exceeded.
    """
    from unittest.mock import AsyncMock, patch
    from app.providers.deepseek import _check_rate_limit

    with patch("app.providers.deepseek.get_counter", new_callable=AsyncMock) as mock_get, \
         patch("app.providers.deepseek.increment_counter", new_callable=AsyncMock) as mock_inc:
        mock_get.return_value = 55  # over limit of 50

        result = await _check_rate_limit()

        assert result is False
        mock_inc.assert_not_called()


@pytest.mark.unit
async def test_circuit_breaker_open_when_failures_exceed_threshold():
    """
    Protects against: circuit breaker not opening after repeated 429 responses,
    causing every subsequent call to still hit the DeepSeek API unnecessarily.
    """
    from unittest.mock import AsyncMock, patch
    from app.providers.deepseek import _check_circuit_breaker, _CB_THRESHOLD

    with patch("app.providers.deepseek.get_counter", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _CB_THRESHOLD  # exactly at threshold → open

        result = await _check_circuit_breaker()

        assert result is False


@pytest.mark.unit
async def test_rate_limit_fails_open_if_redis_unavailable():
    """
    Protects against: rate limiter blocking all DeepSeek calls when Redis is down —
    the check must fail open to avoid cascading failures.
    """
    from unittest.mock import AsyncMock, patch
    from app.providers.deepseek import _check_rate_limit

    with patch("app.providers.deepseek.get_counter", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = Exception("Redis down")

        result = await _check_rate_limit()

        assert result is True  # fail open — never block on Redis failure


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 7: narrator hallucination guardrails
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_extract_numbers_from_text_basic():
    """
    Protects against: _extract_numbers_from_text not parsing Indonesian number format
    (1.234) correctly, causing multi_module validation to miss critical numbers.
    """
    from app.services.narrator_service import _extract_numbers_from_text

    result = _extract_numbers_from_text("terdapat 1.234 sekolah dan 567 guru")
    assert 1234 in result
    assert 567 in result

    result_small = _extract_numbers_from_text("nomor 1 dan 2")
    assert result_small == []


@pytest.mark.unit
def test_contains_hallucination_detects_pattern():
    """
    Protects against: LLM slipping in self-referential phrases ("berdasarkan pengetahuan
    saya", "sebagai AI") that are invisible to number validation but break user trust.
    """
    from app.services.narrator_service import _contains_hallucination

    assert _contains_hallucination("berdasarkan pengetahuan saya, jumlahnya banyak") is True
    assert _contains_hallucination("terdapat 500 sekolah di Jawa Barat") is False
    assert _contains_hallucination("sebagai AI, saya tidak bisa memastikan") is True


@pytest.mark.unit
def test_validate_numbers_passthrough_when_all_present():
    """
    Protects against: _validate_numbers_in_narasi blocking valid narasi or not
    returning the original text when all critical numbers are present.
    """
    from app.services.narrator_service import _validate_numbers_in_narasi

    narasi = "Terdapat 500 sekolah dan 1.234 guru di wilayah ini"
    result = _validate_numbers_in_narasi(narasi, [500, 1234])
    assert result == narasi


@pytest.mark.unit
def test_validate_numbers_fallback_when_number_missing():
    """
    Protects against: _validate_numbers_in_narasi passing narasi through even when
    the LLM dropped a critical number — causing silently wrong responses.
    """
    from app.services.narrator_service import _validate_numbers_in_narasi

    narasi = "Terdapat banyak sekolah di wilayah ini"
    result = _validate_numbers_in_narasi(narasi, {"total": 500})
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 8: Extended inject_from_result (P17-C)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_dependency_detection_kabupaten_level():
    """
    Protects against: kabupaten-level ranking query using provinsi injection
    instead of kabupaten, causing list handler to search at wrong wilayah level.
    """
    intent = _keyword_fallback(
        "kabupaten dengan SMK terbanyak di Jawa Barat tampilkan sekolahnya"
    )

    assert intent.dependencies != {}, "dependencies must be set"
    inject_map = intent.dependencies.get("list", {}).get("inject_from_result", {})
    assert "kabupaten" in inject_map, (
        f"Expected 'kabupaten' key in inject_map, got: {inject_map}"
    )
    assert inject_map["kabupaten"] == "top_location"
    assert "provinsi" not in inject_map, (
        f"inject_map should not have 'provinsi' for kabupaten-level query, got: {inject_map}"
    )


@pytest.mark.unit
def test_dependency_detection_jenjang_inject():
    """
    Protects against: jenjang filter not being propagated to list handler
    when query has both ranking and jenjang intent, causing unfiltered school list.
    """
    intent = _keyword_fallback(
        "provinsi dengan SD terbanyak tampilkan daftarnya"
    )

    assert intent.dependencies != {}, "dependencies must be set"
    inject_map = intent.dependencies.get("list", {}).get("inject_from_result", {})
    assert "jenjang" in inject_map, (
        f"Expected 'jenjang' key in inject_map for SD query, got: {inject_map}"
    )
    assert inject_map["jenjang"] == "top_jenjang"


@pytest.mark.unit
def test_build_injected_intent_kabupaten_field():
    """
    Protects against: _build_injected_intent not setting kabupaten wilayah field
    when inject_map uses {"kabupaten": "top_location"}.
    """
    original = CognitiveIntent(module="list", wilayah=WilayahEntities(kabupaten=None))

    result = _build_injected_intent(
        original,
        {"top_location": "Bandung"},
        {"kabupaten": "top_location"},
    )

    assert result.wilayah.kabupaten == "bandung", (
        f"Expected 'bandung', got '{result.wilayah.kabupaten}'"
    )
    assert result.scope == "kabupaten"
    assert original.wilayah.kabupaten is None


@pytest.mark.unit
def test_build_injected_intent_jenjang_direct_field():
    """
    Protects against: _build_injected_intent failing to inject jenjang into
    intent directly (not into intent.wilayah, which has no jenjang field).
    """
    original = CognitiveIntent(module="list", wilayah=WilayahEntities(), jenjang=None)

    result = _build_injected_intent(
        original,
        {"top_jenjang": "SD"},
        {"jenjang": "top_jenjang"},
    )

    assert result.jenjang == "SD", (
        f"Expected 'SD', got '{result.jenjang}'"
    )
    assert original.jenjang is None


@pytest.mark.unit
def test_build_injected_intent_combined_wilayah_and_jenjang():
    """
    Protects against: combined injection (wilayah + jenjang) partially failing —
    both fields must be injected in a single _build_injected_intent call.
    """
    original = CognitiveIntent(
        module="list",
        wilayah=WilayahEntities(provinsi=None),
        jenjang=None,
    )

    result = _build_injected_intent(
        original,
        {"top_location": "Jawa Timur", "top_jenjang": "SMA"},
        {"provinsi": "top_location", "jenjang": "top_jenjang"},
    )

    assert result.wilayah.provinsi == "jawa timur"
    assert result.jenjang == "SMA"
    assert result.scope == "provinsi"
    assert original.wilayah.provinsi is None
    assert original.jenjang is None
