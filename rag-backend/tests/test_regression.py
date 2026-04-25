# tests/test_regression.py
"""
Regression tests untuk bug yang sudah di-fix di periode Apr 2026.

Run unit tests (CI default, no external deps):
    pytest tests/test_regression.py -v -m unit

Run integration tests (butuh ES hidup):
    RUN_INTEGRATION=1 pytest tests/test_regression.py -v -m integration

Run semua:
    RUN_INTEGRATION=1 pytest tests/test_regression.py -v

Bug classes yang di-cover:
    #1  Aggregation handler — trust intent.scope=nasional (skip resolve_entities)
    #2  Aggregation handler — whitelist keyword "indonesia"/"nasional" di NOT_FOUND path
    #3  Aggregation handler — escape hatch AMBIGUOUS + nasional keyword
    #4  DeepSeek timeout — httpx Timeout per-phase (tidak ditest di sini: non-deterministic)
    #5  main.py — classify_intent timeout 11s → 45s (tidak ditest di sini: integration only)
    #6  Keyword fallback — scope derivation dari wilayah keywords
    #7  Keyword fallback — sub_queries decomposition untuk multi-intent
    #8  Aggregation handler — trust intent.wilayah + skip auto-trust ambiguous city names
"""

import os
import sys
import asyncio
import logging
import pytest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.services.core.cognitive_router import (
    CognitiveIntent,
    WilayahEntities,
    _keyword_fallback,
)
from app.services.query_handlers.aggregation_handler import (
    handle_aggregation_query,
    _AMBIGUOUS_CITIES,
    _normalize_wilayah_prefix,
)
from app.services.core.result_model import ResultStatus


# ─────────────────────────────────────────────────────────────────────────────
# INTEGRATION SKIP GUARD
# ─────────────────────────────────────────────────────────────────────────────

skip_integration = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="Integration tests disabled (set RUN_INTEGRATION=1 to enable)",
)


# ─────────────────────────────────────────────────────────────────────────────
# FIXTURES
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def make_intent():
    """Factory untuk build CognitiveIntent dengan minimal boilerplate."""
    def _make(
        module="aggregation",
        scope="nasional",
        jenjang=None,
        status=None,
        kabupaten=None,
        provinsi=None,
        kecamatan=None,
        desa=None,
        secondary=None,
        sub_queries=None,
    ):
        return CognitiveIntent(
            module=module,
            scope=scope,
            jenjang=jenjang,
            status=status,
            secondary=secondary or [],
            sub_queries=sub_queries or {},
            wilayah=WilayahEntities(
                kabupaten=kabupaten,
                provinsi=provinsi,
                kecamatan=kecamatan,
                desa=desa,
            ),
        )
    return _make


@pytest.fixture
def nasional_es_mock():
    """ES AsyncMock yang return aggregation nasional sukses (only needs .search)."""
    es = AsyncMock()
    es.search.return_value = {
        "aggregations": {
            "total": {"value": 150000},
            "by_provinsi": {
                "buckets": [
                    {"key": "prov. jawa barat", "doc_count": 15000},
                    {"key": "prov. jawa timur", "doc_count": 14000},
                ]
            },
        },
        "hits": {"total": {"value": 150000}, "hits": []},
    }
    return es


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 1 — Keyword Fallback: Scope Detection
# Protects: bug #6 — scope selalu "nasional" meski ada wilayah eksplisit
# ─────────────────────────────────────────────────────────────────────────────

SCOPE_CASES = [
    # (query, expected_scope, expected_jenjang)
    # Nasional — keyword eksplisit
    ("berapa total SD di Indonesia",     "nasional",  "SD"),
    ("jumlah SMK nasional",              "nasional",  "SMK"),
    ("total SD se-Indonesia",            "nasional",  "SD"),
    ("berapa total sekolah",             "nasional",  None),   # no location → default nasional
    # Kabupaten/kota — via _KAB_PATTERNS atau _di_match fallback
    ("total SMA di kota Bandung",        "kabupaten", "SMA"),  # matches r'\bkota\s+\w+'
    ("berapa SMK di Bandung",            "kabupaten", "SMK"),  # _di_match fallback (bug #6)
    ("berapa SMA di Bogor",              "kabupaten", "SMA"),  # _di_match fallback (bug #6)
    # Kecamatan — via _KEC_PATTERNS
    ("jumlah SD di kecamatan Lowokwaru", "kecamatan", "SD"),
]


@pytest.mark.unit
@pytest.mark.parametrize("query,expected_scope,expected_jenjang", SCOPE_CASES)
def test_keyword_fallback_scope_detection(query, expected_scope, expected_jenjang):
    """
    Protects against: keyword fallback defaulting scope to 'nasional' for
    queries with explicit wilayah keyword (bug #6).
    """
    intent = _keyword_fallback(query)
    assert intent.scope == expected_scope, (
        f"Query {query!r}: expected scope={expected_scope!r}, got {intent.scope!r}"
    )
    if expected_jenjang is not None:
        assert intent.jenjang == expected_jenjang, (
            f"Query {query!r}: expected jenjang={expected_jenjang!r}, got {intent.jenjang!r}"
        )


@pytest.mark.unit
@pytest.mark.parametrize("query", [
    "berapa SMK di Bandung",
    "total SD di Bekasi",
    "jumlah SMP di Depok",
    "berapa SMA di Bogor",
])
def test_scope_never_nasional_for_explicit_city(query):
    """
    Protects against: city-level query getting scope='nasional',
    which causes aggregation handler to skip wilayah filter (bug #6).
    """
    intent = _keyword_fallback(query)
    assert intent.scope != "nasional", (
        f"Query {query!r} has explicit city — scope must not be 'nasional', got {intent.scope!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 2 — Keyword Fallback: Sub-Queries Decomposition
# Protects: bug #7 — sub_queries tidak ter-emit untuk multi-intent
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_keyword_fallback_sub_queries_agg_plus_list():
    """
    Protects against: empty sub_queries for multi-intent aggregation+list query (bug #7).
    Without sub_queries, each module receives the full query instead of its focused part.
    """
    query = "berapa total SMK di Bandung dan tampilkan daftarnya"
    intent = _keyword_fallback(query)

    assert intent.module == "aggregation", f"Expected module=aggregation, got {intent.module!r}"
    assert "list" in intent.secondary, (
        f"Expected 'list' in secondary, got {intent.secondary}"
    )
    assert "aggregation" in intent.sub_queries, (
        "Expected sub_queries to contain key 'aggregation'"
    )
    assert "list" in intent.sub_queries, (
        "Expected sub_queries to contain key 'list'"
    )


@pytest.mark.unit
def test_keyword_fallback_sub_queries_comparison_plus_list():
    """
    Protects against: empty sub_queries for comparison+list multi-intent (bug #7).
    Query uses 'daftarnya' to trigger list secondary via keyword signal.
    """
    query = "bandingkan SMA di Surabaya dan Malang, tampilkan daftarnya"
    intent = _keyword_fallback(query)

    assert intent.module == "comparison", f"Expected module=comparison, got {intent.module!r}"
    assert "list" in intent.secondary, (
        f"Expected 'list' in secondary, got {intent.secondary}"
    )
    assert "comparison" in intent.sub_queries, (
        "Expected sub_queries key 'comparison'"
    )
    assert "list" in intent.sub_queries, (
        "Expected sub_queries key 'list'"
    )


@pytest.mark.unit
def test_keyword_fallback_single_intent_has_no_sub_queries():
    """
    Protects against: sub_queries populated for single-intent queries (regression check).
    sub_queries must only be built when there are secondary modules.
    """
    intent = _keyword_fallback("berapa total SD")
    assert intent.module == "aggregation"
    assert intent.secondary == []
    assert intent.sub_queries == {}, (
        f"Single-intent query must have empty sub_queries, got {intent.sub_queries}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 3 — Aggregation Handler: Trust intent.scope=nasional
# Protects: bugs #1 #2 #3 — LOCATION_NOT_FOUND / AMBIGUOUS untuk query nasional
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregation_trusts_nasional_scope_bypasses_resolver(make_intent, nasional_es_mock):
    """
    Protects against: aggregation handler ignoring intent.scope=nasional and calling
    resolve_entities, which returns LOCATION_NOT_FOUND for 'Indonesia' (bugs #1, #2, #3).
    """
    intent = make_intent(scope="nasional", jenjang="SD")

    with patch(
        "app.services.query_handlers.aggregation_handler.resolve_entities",
        new_callable=AsyncMock,
    ) as mock_resolve:
        result = await handle_aggregation_query(
            query="berapa total SD di Indonesia",
            es_client=nasional_es_mock,
            intent=intent,
        )

    mock_resolve.assert_not_called()
    assert result["provider"] == "ELASTICSEARCH_AGGREGATION_NASIONAL", (
        f"Expected ELASTICSEARCH_AGGREGATION_NASIONAL, got {result['provider']!r}"
    )
    assert result["total"] == 150000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregation_nasional_no_jenjang_bypasses_resolver(make_intent, nasional_es_mock):
    """
    Protects against: intent.scope=nasional with no jenjang filter still calling
    resolve_entities (regression for minimal national query, bug #1).

    Note: empty-entity queries may return SYSTEM_VALIDATION (handled by `if not entities:` gate
    before the nasional block) — the key regression guard here is only that the resolver
    is NOT called, not that the result is a successful aggregation.
    """
    intent = make_intent(scope="nasional", jenjang=None)

    with patch(
        "app.services.query_handlers.aggregation_handler.resolve_entities",
        new_callable=AsyncMock,
    ) as mock_resolve:
        await handle_aggregation_query(
            query="berapa total sekolah",
            es_client=nasional_es_mock,
            intent=intent,
        )

    # Core regression guard: resolver must NOT be called for nasional scope
    mock_resolve.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregation_nasional_keyword_overrides_ambiguous_resolution(
    make_intent, nasional_es_mock
):
    """
    Protects against: query 'total SD se-Indonesia' returning SYSTEM_AMBIGUOUS because
    the resolver fuzzy-matches 'Indonesia' to multiple candidates (bug #3).
    With intent.scope=nasional, resolver is never called — this tests the first guard.
    """
    intent = make_intent(scope="nasional", jenjang="SD")

    with patch(
        "app.services.query_handlers.aggregation_handler.resolve_entities",
        new_callable=AsyncMock,
    ) as mock_resolve:
        result = await handle_aggregation_query(
            query="total SD se-Indonesia",
            es_client=nasional_es_mock,
            intent=intent,
        )

    mock_resolve.assert_not_called()
    assert result.get("provider") == "ELASTICSEARCH_AGGREGATION_NASIONAL", (
        f"Expected nasional result, got provider={result.get('provider')!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 4 — Aggregation Handler: Trust Explicit Wilayah Prefix
# Protects: bug #8 — resolver dipanggil padahal wilayah sudah explicit prefix
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregation_trusts_explicit_kota_prefix_skips_resolver(make_intent):
    """
    Protects against: aggregation calling resolve_entities for kabupaten with explicit
    prefix 'kota bandung' — explicit prefix must be auto-trusted (bug #8).
    """
    intent = make_intent(scope="kabupaten", jenjang=None, kabupaten="kota bandung")
    nested_result = {
        "total": 107,
        "breakdown_jenjang": {},
        "breakdown_status": {},
    }

    with (
        patch(
            "app.services.query_handlers.aggregation_handler.resolve_entities",
            new_callable=AsyncMock,
        ) as mock_resolve,
        patch(
            "app.services.query_handlers.aggregation_handler.get_cache",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.set_cache",
            new=AsyncMock(),
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.validate_hierarchy_consistency",
            return_value=True,
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.validate_location_presence",
            return_value=True,
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.execute_nested_aggregation_query",
            new=AsyncMock(return_value=nested_result),
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.execute_drilldown_query",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await handle_aggregation_query(
            query="berapa sekolah di kota bandung",
            es_client=AsyncMock(),
            intent=intent,
        )

    mock_resolve.assert_not_called()
    assert result.get("provider") == "ELASTICSEARCH_AGGREGATION", (
        f"Expected ELASTICSEARCH_AGGREGATION, got {result.get('provider')!r}"
    )
    assert result.get("total") == 107


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregation_trusts_kab_prefix_skips_resolver(make_intent):
    """
    Protects against: 'kab. malang' not being recognized as explicit prefix (bug #8).
    """
    intent = make_intent(scope="kabupaten", jenjang=None, kabupaten="kab. malang")
    nested_result = {"total": 55, "breakdown_jenjang": {}, "breakdown_status": {}}

    with (
        patch(
            "app.services.query_handlers.aggregation_handler.resolve_entities",
            new_callable=AsyncMock,
        ) as mock_resolve,
        patch(
            "app.services.query_handlers.aggregation_handler.get_cache",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.set_cache",
            new=AsyncMock(),
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.validate_hierarchy_consistency",
            return_value=True,
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.validate_location_presence",
            return_value=True,
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.execute_nested_aggregation_query",
            new=AsyncMock(return_value=nested_result),
        ),
        patch(
            "app.services.query_handlers.aggregation_handler.execute_drilldown_query",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await handle_aggregation_query(
            query="berapa sekolah di kab. malang",
            es_client=AsyncMock(),
            intent=intent,
        )

    mock_resolve.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 5 — Aggregation Handler: Approach B — Ambiguous City Skip
# Protects: bug #8 — "bandung" tanpa prefix jangan di-auto-trust
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregation_skips_autotrust_for_ambiguous_city_without_prefix(
    make_intent, caplog
):
    """
    Protects against: auto-resolving 'bandung' (without kab./kota prefix) to one of
    kab. or kota arbitrarily. Approach B: skip auto-trust, fall back to resolver
    so user gets clarification candidates (bug #8).
    """
    caplog.set_level(logging.INFO)

    ambiguous_resolution = {
        "status": ResultStatus.AMBIGUOUS,
        "data": {
            "candidates": [
                {"label": "kota bandung"},
                {"label": "kab. bandung"},
            ],
        },
        "metadata": {},
    }

    intent = make_intent(scope="kabupaten", jenjang="SMK", kabupaten="bandung")

    with patch(
        "app.services.query_handlers.aggregation_handler.resolve_entities",
        new=AsyncMock(return_value=ambiguous_resolution),
    ) as mock_resolve:
        result = await handle_aggregation_query(
            query="berapa SMK di bandung",
            es_client=AsyncMock(),
            intent=intent,
        )

    # Approach B: resolver MUST be called (auto-trust was skipped)
    mock_resolve.assert_called_once()

    # Handler returns AMBIGUOUS back to caller so user can clarify
    assert result.get("provider") == "SYSTEM_AMBIGUOUS", (
        f"Expected SYSTEM_AMBIGUOUS for ambiguous city, got {result.get('provider')!r}"
    )

    # Log must contain the skip-auto-trust decision
    assert any(
        "skip auto-trust" in rec.message.lower()
        or "ambiguous city" in rec.message.lower()
        for rec in caplog.records
    ), (
        "Expected log about skipping auto-trust for ambiguous city name. "
        f"Got records: {[r.message for r in caplog.records if r.levelno >= logging.INFO]}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_aggregation_ambiguous_city_malang_skips_autotrust(make_intent, caplog):
    """
    Protects against: 'malang' (also in _AMBIGUOUS_CITIES) being auto-trusted (bug #8).
    """
    caplog.set_level(logging.INFO)

    ambiguous_resolution = {
        "status": ResultStatus.AMBIGUOUS,
        "data": {"candidates": [{"label": "kota malang"}, {"label": "kab. malang"}]},
        "metadata": {},
    }
    intent = make_intent(scope="kabupaten", jenjang="SD", kabupaten="malang")

    with patch(
        "app.services.query_handlers.aggregation_handler.resolve_entities",
        new=AsyncMock(return_value=ambiguous_resolution),
    ) as mock_resolve:
        result = await handle_aggregation_query(
            query="berapa SD di malang",
            es_client=AsyncMock(),
            intent=intent,
        )

    mock_resolve.assert_called_once()
    assert result.get("provider") == "SYSTEM_AMBIGUOUS"
    assert any(
        "skip auto-trust" in rec.message.lower() or "ambiguous city" in rec.message.lower()
        for rec in caplog.records
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 6 — Aggregation Handler: Private Helpers
# Protects: bug #8 — _AMBIGUOUS_CITIES dan _normalize_wilayah_prefix harus benar
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_ambiguous_cities_set_contains_key_cities():
    """
    Protects against: _AMBIGUOUS_CITIES being empty or missing cities
    that have both kab. and kota variants (bug #8).
    """
    required = {"bandung", "malang", "bogor", "bekasi", "tangerang", "cirebon"}
    missing = required - _AMBIGUOUS_CITIES
    assert not missing, (
        f"_AMBIGUOUS_CITIES is missing cities with kab/kota ambiguity: {missing}"
    )
    assert len(_AMBIGUOUS_CITIES) >= 10, (
        f"Expected >= 10 ambiguous cities, got {len(_AMBIGUOUS_CITIES)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("value,prefix,expected", [
    ("bandung",       "kab.", "kab. bandung"),      # no prefix → add prefix
    ("kota bandung",  "kab.", "kota bandung"),       # already 'kota ' → keep as is
    ("kab. bandung",  "kab.", "kab. bandung"),       # already 'kab.' → keep as is
    ("jawa barat",    "prov.", "prov. jawa barat"),  # no prefix → add prov.
    ("prov. bali",    "prov.", "prov. bali"),        # already 'prov.' → keep as is
    ("lowokwaru",     "kec.", "kec. lowokwaru"),     # no prefix → add kec.
    ("kecamatan sukasari", "kec.", "kecamatan sukasari"),  # already kecamatan → keep
])
def test_normalize_wilayah_prefix(value, prefix, expected):
    """
    Protects against: double-prefix ('kab. kab. bandung') or missing prefix
    in wilayah normalization before Elasticsearch query (bug #8).
    """
    result = _normalize_wilayah_prefix(value, prefix)
    assert result == expected, (
        f"_normalize_wilayah_prefix({value!r}, {prefix!r}) = {result!r}, expected {expected!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP 7 — Integration: Real ES Validation
# Hanya jalan dengan RUN_INTEGRATION=1
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
async def real_es_client():
    """Real ES client — session-scoped agar hanya 1 koneksi per test session."""
    if os.getenv("RUN_INTEGRATION") != "1":
        pytest.skip("Integration tests disabled")

    from elasticsearch import AsyncElasticsearch

    es_url = (
        os.environ.get("ELASTICSEARCH_NODE")
        or os.environ.get("ELASTICSEARCH_URL")
        or "http://localhost:9200"
    )
    if not es_url.startswith("http"):
        es_url = "http://" + es_url

    es_user = os.environ.get("ELASTICSEARCH_USERNAME", "")
    es_pass = os.environ.get("ELASTICSEARCH_PASSWORD", "")
    client = AsyncElasticsearch(
        es_url,
        basic_auth=(es_user, es_pass) if es_user else None,
        request_timeout=15,
    )
    try:
        yield client
    finally:
        await client.close()


@skip_integration
@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_aggregation_nasional_sd(real_es_client, make_intent):
    """
    Integration: verifikasi nasional aggregation SD execute sukses dari real ES index
    dan return total > 10k (realistik untuk data sekolah nasional).
    """
    intent = make_intent(scope="nasional", jenjang="SD")
    result = await handle_aggregation_query(
        query="berapa total SD di Indonesia",
        es_client=real_es_client,
        intent=intent,
    )
    assert result.get("provider") == "ELASTICSEARCH_AGGREGATION_NASIONAL", (
        f"Expected ELASTICSEARCH_AGGREGATION_NASIONAL, got {result.get('provider')!r}"
    )
    assert result.get("total", 0) > 10_000, (
        f"SD di Indonesia harusnya > 10.000, got {result.get('total')}"
    )


@skip_integration
@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_aggregation_nasional_no_intent(real_es_client):
    """
    Integration: query tanpa intent (intent=None) tidak boleh crash atau return hard error.
    Exercises the NOT_FOUND fallback path to national scope.
    """
    result = await handle_aggregation_query(
        query="berapa total sekolah",
        es_client=real_es_client,
        intent=None,
    )
    provider = result.get("provider", "")
    assert provider not in ("SYSTEM_ERROR", "SYSTEM_VALIDATION", None), (
        f"Expected non-error provider, got {provider!r}"
    )
    assert result.get("total", 0) > 0
