# tests/test_regression.py
"""
Regression tests untuk bug yang sudah di-fix di periode Apr–Mei 2026.

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


# ─────────────────────────────────────────────────────────────────────────────
# GROUP Celah-5 — profile_builder nested keys → _extract_profile_fields
# Protects: guru/siswa/tendik data not reaching narrator prompt (Celah 5)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_extract_profile_fields_maps_nested_guru():
    """
    Protects against: _extract_profile_fields ignoring profile_builder nested keys
    guru.total, guru.pns, guru.honor, guru.sertifikasi, guru.s1_keatas,
    siswa.total, tendik.total.
    """
    from app.services.narrator_service import _extract_profile_fields

    profile_data = {
        "guru": {
            "total": 25,
            "laki_laki": 10,
            "perempuan": 15,
            "pns": 18,
            "honor": 7,
            "sertifikasi": 20,
            "s1_keatas": 23,
        },
        "tendik": {
            "total": 5,
            "laki_laki": 3,
            "perempuan": 2,
        },
        "siswa": {
            "total": 500,
            "laki_laki": 250,
            "perempuan": 250,
        },
        "identitas": {"nama": "SDN Test", "npsn": "12345678"},
        "lokasi": {},
        "sarana_prasarana": {},
    }

    f = _extract_profile_fields(profile_data)

    assert f["guru_total"] == 25, f"guru_total expected 25, got {f['guru_total']}"
    assert f["guru_l"]     == 10, f"guru_l expected 10, got {f['guru_l']}"
    assert f["guru_p"]     == 15, f"guru_p expected 15, got {f['guru_p']}"
    assert f["guru_pns"]   == 18, f"guru_pns expected 18, got {f['guru_pns']}"
    assert f["guru_honor"] == 7,  f"guru_honor expected 7, got {f['guru_honor']}"
    assert f["guru_serti"] == 20, f"guru_serti expected 20, got {f['guru_serti']}"
    assert f.get("guru_s1_keatas") == 23, f"guru_s1_keatas expected 23, got {f.get('guru_s1_keatas')}"
    assert f["tendik_total"] == 5,   f"tendik_total expected 5, got {f['tendik_total']}"
    assert f["tendik_l"]     == 3,   f"tendik_l expected 3, got {f['tendik_l']}"
    assert f["tendik_p"]     == 2,   f"tendik_p expected 2, got {f['tendik_p']}"
    assert f["siswa"]        == 500, f"siswa expected 500, got {f['siswa']}"
    assert f["siswa_l"]      == 250, f"siswa_l expected 250, got {f['siswa_l']}"
    assert f["siswa_p"]      == 250, f"siswa_p expected 250, got {f['siswa_p']}"


@pytest.mark.unit
def test_extract_profile_fields_legacy_keys_still_work():
    """
    Protects backward compat: old flat keys (tenaga_pendidik.*, peserta_didik.*)
    still resolve when new nested keys are absent.
    """
    from app.services.narrator_service import _extract_profile_fields

    profile_data = {
        "tenaga_pendidik": {
            "total_guru": 12,
            "laki_laki": 5,
            "perempuan": 7,
            "guru_pns": 8,
            "guru_honor": 4,
            "guru_sertifikasi": 10,
        },
        "peserta_didik": {
            "total_siswa": 300,
            "laki_laki": 160,
            "perempuan": 140,
        },
        "identitas": {"nama": "SMP Legacy", "npsn": "99999999"},
        "lokasi": {},
        "sarana_prasarana": {},
    }

    f = _extract_profile_fields(profile_data)

    assert f["guru_total"] == 12, f"legacy guru_total expected 12, got {f['guru_total']}"
    assert f["siswa"]      == 300, f"legacy siswa expected 300, got {f['siswa']}"


@pytest.mark.unit
def test_prompt_school_profile_no_placeholder_strings():
    """
    Protects against: _prompt_school_profile() emitting 'data tidak tersedia di indeks ini'
    when guru/siswa data is available (Celah 5).
    """
    from app.services.narrator_service import _prompt_school_profile

    profile_data = {
        "guru": {
            "total": 30, "laki_laki": 12, "perempuan": 18,
            "pns": 20, "honor": 10, "sertifikasi": 25, "s1_keatas": 28,
        },
        "siswa": {"total": 600, "laki_laki": 310, "perempuan": 290},
        "tendik": {"total": 6, "laki_laki": 4, "perempuan": 2},
        "identitas": {
            "nama": "SMAN 1 Test", "npsn": "11111111",
            "status_sekolah": "Negeri", "bentuk_pendidikan": "SMA",
            "akreditasi": "A",
        },
        "lokasi": {"kecamatan": "Menteng", "kabupaten": "Jakarta Pusat", "provinsi": "DKI Jakarta"},
        "sarana_prasarana": {},
        "kualitas_data": {"total_masalah": 0, "persen_bersih": 100.0, "qc_labels": {}},
    }

    prompt = _prompt_school_profile(profile_data, query="profil SMAN 1 Test")

    assert "data tidak tersedia di indeks ini" not in prompt, (
        "Placeholder string must not appear in prompt when guru/siswa data is present"
    )
    assert "30" in prompt, "guru_total (30) must appear in prompt"
    assert "600" in prompt, "siswa_total (600) must appear in prompt"


# ─────────────────────────────────────────────────────────────────────────────
# GROUP Celah-2 — context_optimizer profile_rich budget
# Protects: profile context truncated at 3800 chars (single_local) instead of 5500
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_select_budget_returns_profile_rich_for_profile_module():
    """
    Protects against: select_budget falling through to single_local (3800 chars)
    for profile queries instead of using profile_rich (5500 chars).
    """
    from app.services.core.context_optimizer import select_budget, _BUDGET_CONFIGS

    class _ProfileIntent:
        module = "profile"
        scope  = "kabupaten"

    budget = select_budget(_ProfileIntent(), secondary_modules=[])
    assert budget is _BUDGET_CONFIGS["profile_rich"], (
        f"Expected profile_rich budget, got primary_max={budget.primary_max}"
    )
    assert budget.primary_max == 5500, (
        f"profile_rich.primary_max must be 5500, got {budget.primary_max}"
    )


@pytest.mark.unit
def test_select_budget_profile_with_secondary_uses_multi_module():
    """
    Protects: profile_rich must NOT be used when secondary_modules is present.
    """
    from app.services.core.context_optimizer import select_budget, _BUDGET_CONFIGS

    class _ProfileIntent:
        module = "profile"
        scope  = "kabupaten"

    budget = select_budget(_ProfileIntent(), secondary_modules=["list"])
    assert budget is _BUDGET_CONFIGS["multi_module"], (
        f"Expected multi_module when secondary present, got primary_max={budget.primary_max}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP Celah-1 — QC_CONTEXT_LIBRARY injected into narrator prompt
# Protects: narrator prompt explains WHY a field is problematic, not just THAT
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_qc_context_library_injected_when_masalah_present():
    """
    Protects against: _prompt_school_profile() not injecting QC_CONTEXT_LIBRARY
    context when there are bermasalah fields (Celah 1).
    The prompt must contain the penyebab_umum text for each masalah field.
    """
    from app.services.narrator_service import _prompt_school_profile, QC_CONTEXT_LIBRARY

    profile_data = {
        "guru": {"total": 10, "laki_laki": 5, "perempuan": 5, "pns": 8, "honor": 2, "sertifikasi": 7, "s1_keatas": 9},
        "siswa": {"total": 200, "laki_laki": 100, "perempuan": 100},
        "tendik": {"total": 3, "laki_laki": 2, "perempuan": 1},
        "identitas": {
            "nama": "SDN QC Test", "npsn": "22222222",
            "status_sekolah": "Negeri", "bentuk_pendidikan": "SD", "akreditasi": "B",
        },
        "lokasi": {"kecamatan": "Bojong", "kabupaten": "Bogor", "provinsi": "Jawa Barat"},
        "sarana_prasarana": {},
        "kualitas_data": {
            "total_masalah": 2,
            "persen_bersih": 84.62,
            "qc_labels": {
                "qc_1": "OK", "qc_2": "OK", "qc_3": "OK", "qc_4": "OK",
                "qc_5": "OK",
                "qc_6": "Bermasalah (kode: 0)",
                "qc_7": "Bermasalah (kode: 0)",
                "qc_8": "OK", "qc_9": "OK", "qc_10": "OK",
                "qc_11": "OK", "qc_12": "OK", "qc_13": "OK",
            },
        },
    }

    prompt = _prompt_school_profile(profile_data)

    # QC_CONTEXT_LIBRARY penyebab for qc_6 (koordinat lintang) must appear
    qc6_ctx = QC_CONTEXT_LIBRARY["qc_6"]
    assert qc6_ctx["penyebab_umum"][:30] in prompt, (
        f"QC context for qc_6 penyebab must appear in prompt. Got prompt snippet:\n"
        f"{prompt[-500:]}"
    )
    # "Jelaskan MENGAPA" instruction must appear
    assert "MENGAPA" in prompt, "Narrator must be instructed to explain WHY (MENGAPA)"


# ─────────────────────────────────────────────────────────────────────────────
# GROUP Celah-4 — desa ambiguity detection + OR filter
# Protects: enrich_hierarchy silently picking wrong desa when name is ambiguous
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_enrich_hierarchy_flags_ambiguous_desa():
    """
    Protects against: enrich_hierarchy_from_index silently picking entries[0]
    when a desa name exists in multiple kecamatan/kabupaten (Celah 4).
    When len(entries) > 1, result must contain _desa_ambiguous=True and _desa_candidates.
    """
    from app.services.location_service import enrich_hierarchy_from_index, LOCATION_HIERARCHY

    desa_key = "_test_ambiguous_desa_xyz"
    fake_entries = [
        {"kecamatan": "kec. alpha", "kabupaten": "kab. satu", "provinsi": "prov. jawa barat"},
        {"kecamatan": "kec. beta",  "kabupaten": "kab. dua",  "provinsi": "prov. jawa tengah"},
    ]
    if "desa_kelurahan" not in LOCATION_HIERARCHY:
        LOCATION_HIERARCHY["desa_kelurahan"] = {}
    LOCATION_HIERARCHY["desa_kelurahan"][desa_key] = fake_entries

    try:
        result = enrich_hierarchy_from_index({"desa_kelurahan": desa_key})

        assert result.get("_desa_ambiguous") is True, (
            f"Expected _desa_ambiguous=True, got {result.get('_desa_ambiguous')!r}"
        )
        assert "_desa_candidates" in result, "_desa_candidates must be set"
        assert len(result["_desa_candidates"]) == 2, (
            f"Expected 2 candidates, got {len(result['_desa_candidates'])}"
        )
        # Fallback hierarchy from entries[0] must still be set
        assert result.get("kecamatan") == "kec. alpha", (
            f"Fallback kecamatan from entries[0] expected 'kec. alpha', got {result.get('kecamatan')!r}"
        )
    finally:
        del LOCATION_HIERARCHY["desa_kelurahan"][desa_key]


@pytest.mark.unit
def test_build_filters_uses_wilayah_key_or_for_ambiguous_desa():
    """
    Protects against: build_filters generating single desa_kelurahan term filter
    instead of OR over wilayah_key when _desa_ambiguous=True (Celah 4).
    """
    from app.services.core.aggregation_core import build_filters

    entities = {
        "desa_kelurahan": "sumberagung",
        "_desa_ambiguous": True,
        "_desa_candidates": [
            {"provinsi": "prov. jawa timur", "kabupaten": "kab. malang",  "kecamatan": "kec. kepanjen"},
            {"provinsi": "prov. jawa timur", "kabupaten": "kab. blitar",  "kecamatan": "kec. gandusari"},
        ],
    }

    filters = build_filters(entities)
    filter_json = str(filters)

    # Must NOT have a plain desa_kelurahan term filter
    assert '"desa_kelurahan"' not in filter_json or 'wilayah_key' in filter_json, (
        "When _desa_ambiguous, desa_kelurahan term filter must be replaced by wilayah_key OR filter"
    )
    # Must have wilayah_key OR filter
    assert "wilayah_key" in filter_json, (
        f"Expected wilayah_key in filters, got:\n{filter_json}"
    )
    # Expected wilayah_key values
    assert "prov. jawa timur|kab. malang|kec. kepanjen|sumberagung" in filter_json, (
        "wilayah_key candidate 1 must appear in filters"
    )
    assert "prov. jawa timur|kab. blitar|kec. gandusari|sumberagung" in filter_json, (
        "wilayah_key candidate 2 must appear in filters"
    )


# ─────────────────────────────────────────────────────────────────────────────
# GROUP Celah-3 — retrieval retry handler
# Protects: low-retrieval score failing immediately without attempting scope expansion
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_handler_expands_scope_when_low_score():
    """
    Protects against: low retrieval score (<0.3) returning 'data tidak cukup'
    without attempting expanded/keyword_only retrieval (Celah 3).
    When initial search returns empty but expanded scope returns hits,
    retry_with_expanded_query must return those hits with was_retried=True.
    """
    from unittest.mock import AsyncMock, patch
    from app.background.retrieval_retry_handler import retry_with_expanded_query

    fake_hits = [{"_source": {"content": "SDN test content"}, "_score": 1.5}]

    async def _mock_perform_search(es_client, query_text, size, filter_level, location_filter):
        if location_filter is None or "kecamatan" not in (location_filter or {}):
            return fake_hits
        return []

    def _mock_score(hits):
        return 0.5 if hits else 0.0

    with patch(
        "app.background.retrieval_retry_handler.retry_with_expanded_query.__module__"
    ):
        pass  # no-op context to allow deeper patching

    with patch(
        "app.services.search.search_service.perform_search",
        side_effect=_mock_perform_search,
    ), patch(
        "app.services.search.search_service.compute_context_sufficiency_score",
        side_effect=_mock_score,
    ):
        hits, was_retried = await retry_with_expanded_query(
            es_client=AsyncMock(),
            query="berapa SD di desa cibunar kecamatan parungpanjang",
            original_loc_filter={"kecamatan": "kec. parungpanjang", "kabupaten": "kab. bogor"},
            size=5,
            score_before=0.1,
        )

    assert was_retried is True, f"Expected was_retried=True, got {was_retried}"
    assert len(hits) > 0, "Expected non-empty hits after retry"


@pytest.mark.unit
def test_retry_handler_build_expanded_filter_removes_kecamatan():
    """
    Protects: _build_expanded_filter must remove kecamatan but keep kabupaten/provinsi.
    """
    from app.background.retrieval_retry_handler import _build_expanded_filter

    result = _build_expanded_filter({
        "kecamatan": "kec. parungpanjang",
        "kabupaten": "kab. bogor",
        "provinsi": "prov. jawa barat",
    })
    assert "kecamatan" not in result, "kecamatan must be removed in expanded filter"
    assert result.get("kabupaten") == "kab. bogor", "kabupaten must be kept"
    assert result.get("provinsi") == "prov. jawa barat", "provinsi must be kept"


@pytest.mark.unit
def test_retry_handler_expanded_filter_returns_none_when_only_kecamatan():
    """
    Protects: when filter only has kecamatan, expanded filter should be None
    (trigger national scope in attempt 1).
    """
    from app.background.retrieval_retry_handler import _build_expanded_filter

    result = _build_expanded_filter({"kecamatan": "kec. parungpanjang"})
    assert result is None, f"Expected None when filter is kecamatan-only, got {result}"
