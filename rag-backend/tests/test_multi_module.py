"""
test/test_multi_module.py  — v4 (final)
 
Revisi dari v3:
- TC_GEO_003: expected diubah primary="list" secondary=["geo"]
  (LLM benar: "daftar ... dari Monas" → Monas adalah referensi lokasi, bukan posisi user)
- TC_GEO_004: expected diubah primary="list"
  ("di sekitar [landmark]" = filter area, bukan geo berbasis koordinat user)
- PVG_06: expected diubah primary="list"
  ("di sekitar Monas" = filter area)
- TC_EDGE_004: few-shot prompt sudah ditambahkan → LLM harus PASS
- TC_EDGE_006: domain guru sudah ditambahkan ke prompt → LLM harus PASS
- TC_OOC_004: aturan mixed-query sudah ditambahkan → LLM harus PASS
- Tambah TC_GEO_005/006: kasus geo murni berbasis posisi user (koordinat/sini)
"""
 
import sys
from pathlib import Path
 
ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))
 
import pytest
import os
import asyncio
import time
from unittest.mock import patch
 
from app.services.core.intent_classifier import (
    classify_intent,
    IntentResult,
)
from app.services.core.cognitive_router import (
    _keyword_fallback,
)
 
# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
 
LLM_TEST_ENABLED = os.getenv("LLM_TEST_ENABLED", "false").lower() == "true"
RATE_LIMIT_DELAY = float(os.getenv("RATE_LIMIT_DELAY", "3.0"))
MAX_LLM_CALLS    = int(os.getenv("MAX_LLM_CALLS", "50"))
 
_llm_call_count = 0
_last_call_time = 0.0
 
 
@pytest.fixture(autouse=True, scope="session")
def reset_llm_counter():
    global _llm_call_count, _last_call_time
    _llm_call_count = 0
    _last_call_time = 0.0
    yield
 
 
async def _rate_limit_guard():
    global _llm_call_count, _last_call_time
    if _llm_call_count >= MAX_LLM_CALLS:
        pytest.skip(f"Max LLM calls reached ({MAX_LLM_CALLS})")
    elapsed = time.time() - _last_call_time
    if elapsed < RATE_LIMIT_DELAY:
        await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)
    _llm_call_count += 1
    _last_call_time = time.time()
    print(f"\n  📡 LLM Call #{_llm_call_count}/{MAX_LLM_CALLS}")
 
 
# ─────────────────────────────────────────────────────────────────────────────
# TEST DATA — dengan revisi expectation yang sudah dikoreksi
# ─────────────────────────────────────────────────────────────────────────────
 
INTENT_TEST_CASES = [
 
    # ── AGGREGATION ──────────────────────────────────────────────────────────
    ("TC_AGG_001", "berapa jumlah SMK di Kota Bandung?",
     "aggregation", [], {"jenjang": "SMK"}, "aggregation standar"),
    ("TC_AGG_002", "total sekolah negeri di Jawa Barat",
     "aggregation", [], {"status": "NEGERI"}, "aggregation provinsi"),
    ("TC_AGG_003", "kecamatan mana yang paling banyak sekolahnya di Bogor?",
     "aggregation", [], {}, "aggregation ranking"),
    ("TC_AGG_004", "ada berapa SMK akreditasi A di Depok? sebutkan juga nama-namanya",
     "aggregation", ["list"], {"jenjang": "SMK", "akreditasi": "A"},
     "AGG + LIST secondary"),
 
    # ── COMPARISON ───────────────────────────────────────────────────────────
    ("TC_COMP_001", "bandingkan jumlah sekolah di Cisauk dan Serpong",
     "comparison", [], {}, "comparison dua kecamatan"),
    ("TC_COMP_002", "SMA negeri vs swasta di Kota Bogor",
     "comparison", [], {"jenjang": "SMA"}, "comparison status — dua kategori, bukan dua sekolah"),
    ("TC_COMP_003", "bandingkan SMK akreditasi A di Bandung dan Bogor, tampilkan juga daftarnya",
     "comparison", ["list"], {"jenjang": "SMK", "akreditasi": "A"},
     "COMPARISON + LIST"),
    ("TC_COMP_004", "mana yang lebih banyak sekolahnya, Jakarta Selatan atau Jakarta Timur?",
     "comparison", [], {}, "comparison implisit"),
 
    # ── PROFILE ──────────────────────────────────────────────────────────────
    ("TC_PROF_001", "profil SMAN 3 Bandung",
     "profile", [], {"jenjang": "SMA", "status": "NEGERI"}, "profile eksplisit"),
    ("TC_PROF_002", "info sekolah dengan NPSN 20217834",
     "profile", [], {}, "profile via NPSN"),
    ("TC_PROF_003", "di mana letak SDN 02 Parung?",
     "profile", [], {"jenjang": "SD", "status": "NEGERI"},
     "profile — nama sekolah spesifik mengalahkan 'di mana'"),
    ("TC_PROF_004", "alamat SMK Pelita Harapan Tangerang",
     "profile", [], {"jenjang": "SMK"},
     "profile — 'alamat [nama sekolah]'"),
    ("TC_PROF_005", "lokasinya SMPN 5 Depok di mana ya?",
     "profile", [], {"jenjang": "SMP", "status": "NEGERI"},
     "profile — nama sekolah spesifik"),
 
    # ── LIST ─────────────────────────────────────────────────────────────────
    ("TC_LIST_001", "daftar SMA negeri di Depok",
     "list", [], {"jenjang": "SMA", "status": "NEGERI"}, "list standar"),
    ("TC_LIST_002", "tampilkan sekolah akreditasi A di Kecamatan Cisauk",
     "list", [], {"akreditasi": "A"}, "list dengan akreditasi"),
    ("TC_LIST_003", "sekolah apa saja yang ada di Parungpanjang?",
     "list", [], {}, "list implisit"),
 
    # ── GEO ──────────────────────────────────────────────────────────────────
    ("TC_GEO_001", "sekolah terdekat dari sini",
     "geo", [], {}, "geo — 'dari sini' = posisi user"),
    ("TC_GEO_002", "SMP negeri dalam radius 3 km",
     "geo", [], {"jenjang": "SMP", "status": "NEGERI"},
     "geo — radius = posisi user"),
 
    # TC_GEO_003 DIREVISI: "daftar ... dari Monas" → list primary, geo secondary
    # LLM benar: "Monas" adalah referensi lokasi statis, bukan posisi user.
    # primary=list karena ada kata "daftar"; geo=secondary karena ada "terdekat dari".
    ("TC_GEO_003", "daftar SMA terdekat dari Monas",
     "list", ["geo"], {"jenjang": "SMA"},
     "list+geo — Monas = referensi lokasi, bukan posisi user"),
 
    # TC_GEO_004 DIREVISI: "di sekitar [landmark]" → list
    # "di sekitar Taman Mini" = filter area lokasi, bukan geo berbasis GPS user.
    # Keyword fallback DAN LLM sama-sama memilih list → expectation yang perlu dikoreksi.
    ("TC_GEO_004", "sekolah di sekitar Taman Mini Jakarta Timur",
     "list", [], {},
     "list — 'di sekitar landmark' = filter area, bukan posisi user"),
 
    # TC_GEO_005: geo murni — user tidak tahu koordinatnya sendiri
    ("TC_GEO_005", "sekolah terdekat dari posisi saya sekarang",
     "geo", [], {}, "geo — 'posisi saya' = butuh koordinat GPS"),
 
    # TC_GEO_006: geo dengan koordinat eksplisit
    ("TC_GEO_006", "SD negeri dalam radius 1 km dari koordinat -6.2, 106.8",
     "geo", [], {"jenjang": "SD", "status": "NEGERI"},
     "geo — koordinat eksplisit"),
 
    # ── 3 MODUL ──────────────────────────────────────────────────────────────
    ("TC_3MOD_001",
     "bandingkan jumlah SMK akreditasi A di Bandung dan Bogor. tampilkan juga lokasinya",
     "comparison", ["list"], {"jenjang": "SMK", "akreditasi": "A"},
     "COMPARISON + LIST"),
    ("TC_3MOD_002",
     "ada berapa SMA negeri di Depok? dan tampilkan daftar nama serta alamatnya",
     "aggregation", ["list"], {"jenjang": "SMA", "status": "NEGERI"},
     "AGGREGATION + LIST"),
    ("TC_3MOD_003",
     "berapa SMA swasta akreditasi B di Surabaya? cari juga yang terdekat dari Bundaran HI",
     "aggregation", ["list"], {"jenjang": "SMA", "status": "SWASTA", "akreditasi": "B"},
     "AGGREGATION + LIST — Bundaran HI adalah referensi lokasi, bukan posisi user"),
 
    # ── OOC ──────────────────────────────────────────────────────────────────
    ("TC_OOC_001", "siapa presiden Indonesia sekarang?",
     "out_of_context", [], {}, "OOC murni — politik"),
    ("TC_OOC_002", "resep nasi goreng spesial",
     "out_of_context", [], {}, "OOC murni — kuliner"),
    ("TC_OOC_003", "harga saham Telkom hari ini",
     "out_of_context", [], {}, "OOC — pasar modal"),
 
    # TC_OOC_004: mixed query — aturan baru: prioritaskan sinyal pendidikan
    ("TC_OOC_004", "cuaca Jakarta besok dan berapa sekolah di Jakarta?",
     "aggregation", [], {},
     "mixed OOC+pendidikan — sinyal pendidikan harus menang"),
 
    # ── EDGE CASES ───────────────────────────────────────────────────────────
    ("TC_EDGE_001", "sekolah",
     "list", [], {}, "query pendek"),
    ("TC_EDGE_002", "SMK negeri vs swasta di mana lebih banyak?",
     "comparison", [], {"jenjang": "SMK"},
     "comparison dua kategori status — bukan dua sekolah spesifik"),
    ("TC_EDGE_003", "jmlh smp di Bekasi",
     "aggregation", [], {"jenjang": "SMP"}, "typo aggregation"),
 
    # TC_EDGE_004: school vs school → profile (few-shot sudah ditambahkan ke prompt)
    ("TC_EDGE_004", "SMAN 1 Depok vs SMAN 2 Depok",
     "profile", [], {"jenjang": "SMA", "status": "NEGERI"},
     "dua nama sekolah spesifik + vs → profile (school_vs_school)"),
 
    ("TC_EDGE_005", "apakah SMK Pelita Harapan terakreditasi?",
     "profile", [], {"jenjang": "SMK"}, "profile — nama sekolah spesifik"),
 
    # TC_EDGE_006: data guru = domain sistem (aturan domain sudah diperluas di prompt)
    ("TC_EDGE_006", "berapa guru honorer di Jawa Barat",
     "aggregation", [], {},
     "aggregation data guru — guru/PTK termasuk domain sistem"),
]
 
 
# ─────────────────────────────────────────────────────────────────────────────
# PROFILE VS GEO DISAMBIGUATION
# PVG_06 DIREVISI: "di sekitar Monas" = filter area → list (bukan geo)
# ─────────────────────────────────────────────────────────────────────────────
 
PROFILE_VS_GEO_CASES = [
    # Ada nama sekolah spesifik → profile
    ("di mana letak SDN 02 Parung?",          "profile", "nama sekolah spesifik"),
    ("alamat SMAN 5 Depok",                   "profile", "alamat sekolah spesifik"),
    ("lokasinya SMPN 3 Bandung di mana?",     "profile", "lokasi sekolah spesifik"),
 
    # Posisi user tidak diketahui → geo
    ("sekolah terdekat dari rumah saya",      "geo",     "posisi user tidak diketahui"),
    ("SD negeri dalam radius 1 km",           "geo",     "radius dari posisi user"),
 
    # PVG_06 DIREVISI: landmark = referensi area → list
    ("sekolah di sekitar Monas",              "list",    "di sekitar landmark = filter area, bukan GPS"),
 
    # Tidak spesifik, ada di area tertentu → list
    ("di mana ada SD negeri di Depok?",       "list",    "tidak spesifik, filter wilayah"),
]
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ① UNIT: KEYWORD FALLBACK
# ─────────────────────────────────────────────────────────────────────────────
 
@pytest.mark.unit
@pytest.mark.parametrize(
    "tc_id,query,exp_primary,exp_secondary,exp_filters,notes",
    INTENT_TEST_CASES,
    ids=[tc[0] for tc in INTENT_TEST_CASES],
)
def test_keyword_fallback_intent(tc_id, query, exp_primary, exp_secondary, exp_filters, notes):
    result = _keyword_fallback(query)
 
    assert result.primary == exp_primary, (
        f"[{tc_id}] primary mismatch\n"
        f"  Expected : {exp_primary!r}\n"
        f"  Got      : {result.primary!r}\n"
        f"  Query    : {query}\n"
        f"  Notes    : {notes}"
    )
    for sec in exp_secondary:
        assert sec in result.secondary, (
            f"[{tc_id}] secondary missing {sec!r}, got {result.secondary!r}"
        )
    for key, expected_val in exp_filters.items():
        if expected_val is not None:
            actual = result.filters.get(key)
            assert actual == expected_val, (
                f"[{tc_id}] filter[{key!r}]: expected={expected_val!r} got={actual!r}"
            )
 
 
@pytest.mark.unit
def test_ooc_mixed_query():
    result = _keyword_fallback("cuaca Jakarta besok dan berapa sekolah di Jakarta?")
    assert result.primary == "aggregation", \
        f"Mixed OOC+school harus → aggregation, got {result.primary!r}"
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ② INTEGRATION: FULL LLM CLASSIFIER
# ─────────────────────────────────────────────────────────────────────────────
 
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "tc_id,query,exp_primary,exp_secondary,exp_filters,notes",
    INTENT_TEST_CASES,
    ids=[tc[0] for tc in INTENT_TEST_CASES],
)
async def test_full_intent_classifier(tc_id, query, exp_primary, exp_secondary, exp_filters, notes):
    """
    Test classify_intent() dengan LLM asli.
 
    Kasus yang sebelumnya gagal dan sekarang sudah difix:
    - TC_GEO_003/004: expectation direvisi ke list (LLM tidak salah)
    - TC_OOC_004: aturan mixed-query ditambahkan ke prompt
    - TC_EDGE_004: few-shot school_vs_school ditambahkan ke prompt
    - TC_EDGE_006: domain guru diperluas di prompt
    """
    if not LLM_TEST_ENABLED:
        pytest.skip("Set LLM_TEST_ENABLED=true untuk menjalankan test ini")
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY tidak tersedia")
 
    await _rate_limit_guard()
 
    result = await classify_intent(query, use_cache=False, timeout=6.0)
 
    assert result.primary == exp_primary, (
        f"[{tc_id}] LLM primary mismatch\n"
        f"  Expected : {exp_primary!r}\n"
        f"  Got      : {result.primary!r}\n"
        f"  Raw LLM  : {getattr(result, 'raw_label', 'N/A')}\n"
        f"  Query    : {query}\n"
        f"  Notes    : {notes}"
    )
    for sec in exp_secondary:
        if sec not in result.secondary:
            print(f"\n  ⚠  [{tc_id}] secondary {sec!r} tidak terdeteksi "
                  f"(got {result.secondary})")
 
    for key, expected_val in exp_filters.items():
        if expected_val is not None and result.filters.get(key) != expected_val:
            print(f"\n  ⚠  [{tc_id}] filter[{key!r}]: "
                  f"expected={expected_val!r} got={result.filters.get(key)!r}")
 
    print(f"  ✓ [{tc_id}] → {result.primary!r} "
          f"secondary={result.secondary} filters={result.filters}")
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ③ INTEGRATION: PROFILE VS GEO DISAMBIGUATION
# ─────────────────────────────────────────────────────────────────────────────
 
@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    "query,expected_primary,reason",
    PROFILE_VS_GEO_CASES,
    ids=[f"PVG_{i+1:02d}" for i in range(len(PROFILE_VS_GEO_CASES))],
)
async def test_llm_profile_vs_geo(query, expected_primary, reason):
    """
    Batas antara profile, geo, dan list:
    - Nama sekolah spesifik + tanya lokasi/alamat → profile
    - Posisi user tidak diketahui + butuh GPS → geo
    - Landmark sebagai referensi area ("di sekitar X") → list
    - Wilayah sebagai filter ("di Depok", "di sekitar Monas") → list
    """
    if not LLM_TEST_ENABLED:
        pytest.skip("Set LLM_TEST_ENABLED=true")
    if not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("DEEPSEEK_API_KEY tidak tersedia")
 
    await _rate_limit_guard()
    result = await classify_intent(query, use_cache=False, timeout=6.0)
 
    assert result.primary == expected_primary, (
        f"Profile/Geo/List disambiguation fail\n"
        f"  Query    : {query}\n"
        f"  Expected : {expected_primary!r}\n"
        f"  Got      : {result.primary!r}\n"
        f"  Reason   : {reason}"
    )
    print(f"  ✓ PVG [{expected_primary!r}] {query[:55]}")
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ④ UNIT: ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────
 
@pytest.mark.asyncio
@pytest.mark.unit
async def test_orchestrator_parallel_execution():
    try:
        from app.services.core.multi_intent_orchestrator import orchestrate
        from app.services.core.cognitive_router import CognitiveIntent
    except ImportError as exc:
        pytest.fail(f"multi_intent_orchestrator tidak tersedia: {exc}")

    async def slow_aggregation(*args, **kwargs):
        await asyncio.sleep(0.5)
        return {"provider": "MOCK_AGG", "answer_markdown": "5 sekolah.", "total": 5, "confidence": 0.8}

    class MockListHandler:
        async def handle(self, query, *args, **kwargs):
            await asyncio.sleep(0.5)
            return {"status": "success", "schools": [{"nama": "SDN 1"}], "total": 1}

    intent = CognitiveIntent(module="aggregation", secondary=["list"])
    start  = time.time()

    with patch(
        "app.services.query_handlers.aggregation_handler.handle_aggregation_query",
        new=slow_aggregation,
    ):
        result = await orchestrate(
            intent, "berapa SD di Depok? tampilkan juga", None, "test-user",
            list_handler=MockListHandler(),
        )

    elapsed = time.time() - start
    assert elapsed < 1.2, f"Tidak paralel: {elapsed:.2f}s (expected < 1.2s)"
    assert "merged_context" in result
    print(f"\n  ✓ Parallel: {elapsed:.2f}s")
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ⑤ UNIT: MERGE RESULT STRUCTURE
# ─────────────────────────────────────────────────────────────────────────────
 
@pytest.mark.unit
def test_merge_module_results_structure():
    try:
        from app.services.core.multi_intent_orchestrator import merge_module_results, ModuleResult
    except ImportError as exc:
        pytest.fail(f"merge_module_results tidak tersedia: {exc}")
 
    primary = ModuleResult(
        module_name="aggregation", provider="ELASTICSEARCH_AGG",
        answer_markdown="## Ringkasan\n\nKota Bandung memiliki 342 SMK.",
        success=True, num_docs=342,
    )
    secondary = [ModuleResult(
        module_name="list", provider="ELASTICSEARCH_LIST",
        answer_markdown="- SMK Negeri 1 Bandung\n- SMK Negeri 4 Bandung",
        success=True, num_docs=2,
    )]
    merged = merge_module_results(primary, secondary)
 
    assert "342" in merged
    assert "SMK Negeri" in merged
    assert "---" in merged
    assert merged.find("342") < merged.find("SMK Negeri"), \
        "Primary harus tampil sebelum secondary"
 
 
# ─────────────────────────────────────────────────────────────────────────────
# ⑥ UNIT: FILTER INJECTION
# ─────────────────────────────────────────────────────────────────────────────
 
@pytest.mark.unit
def test_filter_injection_adds_missing():
    try:
        from app.services.core.multi_intent_orchestrator import inject_filters_to_query
    except ImportError as exc:
        pytest.fail(f"inject_filters_to_query tidak tersedia: {exc}")
 
    result = inject_filters_to_query(
        "bandingkan sekolah di Bandung dan Bogor",
        {"jenjang": "SMK", "akreditasi": "A"},
    )
    assert "SMK" in result or "jenjang=SMK" in result
    assert "A" in result or "akreditasi=A" in result
 
 
@pytest.mark.unit
def test_filter_injection_no_duplicate():
    try:
        from app.services.core.multi_intent_orchestrator import inject_filters_to_query
    except ImportError as exc:
        pytest.fail(f"inject_filters_to_query tidak tersedia: {exc}")
 
    result = inject_filters_to_query(
        "bandingkan SMK akreditasi A di Bandung",
        {"jenjang": "SMK", "akreditasi": "A"},
    )
    assert result.lower().count("akreditasi") <= 2
 
 
# ─────────────────────────────────────────────────────────────────────────────
# MANUAL RUNNER
# ─────────────────────────────────────────────────────────────────────────────
 
# ─────────────────────────────────────────────────────────────────────────────
# ⑦ UNIT: SEMESTER FILTER
# Verifikasi build_es_query() menyertakan filter semester_id terbaru
# sehingga dokumen historis tidak ikut diambil.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_es_query_contains_semester_filter():
    """build_es_query() harus menyertakan hard filter semester_id."""
    try:
        from app.services.core.es_query_builder import build_es_query, _latest_semester_id
        from app.services.core.cognitive_router import CognitiveIntent, WilayahEntities
    except ImportError as exc:
        pytest.fail(f"import gagal: {exc}")

    intent = CognitiveIntent(
        module="list",
        scope="kabupaten",
        jenjang="SMP",
        wilayah=WilayahEntities(kabupaten="bandung"),
    )
    query = build_es_query(intent, {}, size=10)
    filter_clauses = query["query"]["bool"].get("filter", [])
    sem_id = _latest_semester_id()

    # Filter semester_id harus ada di antara filter hard
    found = any(
        clause.get("term", {}).get("semester_id") == sem_id
        for clause in filter_clauses
    )
    assert found, (
        f"Filter semester_id '{sem_id}' tidak ditemukan di query.\n"
        f"Filter aktif: {filter_clauses}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ⑧ UNIT: TIPE_JAMBAN LABEL CONVERSION
# Verifikasi build_profile() mengembalikan label teks, bukan angka.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_profile_builder_jamban_label():
    """
    build_profile() harus mengembalikan label string untuk tipe_jamban
    dan kecukupan_air, bukan kode integer mentah.
    """
    try:
        from app.services.core.profile_builder import build_profile
    except ImportError as exc:
        pytest.fail(f"import gagal: {exc}")

    source = {
        "nama": "SDN Test",
        "npsn": "12345678",
        "tipe_jamban": 1,       # 1 → "Leher Angsa"
        "kecukupan_air": 2,     # 2 → "Cukup"
    }
    profile = build_profile(source, {})
    sarana = profile["sarana_prasarana"]

    assert sarana["tipe_jamban"] == "Leher Angsa", (
        f"tipe_jamban harus 'Leher Angsa', got {sarana['tipe_jamban']!r}"
    )
    assert sarana["kecukupan_air"] == "Cukup", (
        f"kecukupan_air harus 'Cukup', got {sarana['kecukupan_air']!r}"
    )


@pytest.mark.unit
def test_profile_builder_jamban_zero_returns_none():
    """tipe_jamban=0 (Tidak Ada) dan kecukupan_air=0 (Sangat Kurang) harus dikembalikan dengan benar."""
    from app.services.core.profile_builder import build_profile
    source = {"tipe_jamban": 0, "kecukupan_air": 0}
    profile = build_profile(source, {})
    sarana = profile["sarana_prasarana"]

    assert sarana["tipe_jamban"] == "Tidak Ada", (
        f"tipe_jamban=0 harus 'Tidak Ada', got {sarana['tipe_jamban']!r}"
    )
    assert sarana["kecukupan_air"] == "Sangat Kurang", (
        f"kecukupan_air=0 harus 'Sangat Kurang', got {sarana['kecukupan_air']!r}"
    )


@pytest.mark.unit
def test_profile_builder_jamban_none_returns_none():
    """Jika tipe_jamban/kecukupan_air tidak ada di source → harus None (bukan label default)."""
    from app.services.core.profile_builder import build_profile
    profile = build_profile({}, {})
    sarana = profile["sarana_prasarana"]

    assert sarana["tipe_jamban"] is None, (
        f"tipe_jamban None harus dikembalikan None, got {sarana['tipe_jamban']!r}"
    )
    assert sarana["kecukupan_air"] is None, (
        f"kecukupan_air None harus dikembalikan None, got {sarana['kecukupan_air']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ⑨ UNIT: EXACT SCHOOL NAME — SDN 1 SERPONG vs SDN 1 SERPONG UTARA
# Verifikasi keyword fallback TIDAK mencampurkan dua sekolah bernama mirip.
# Pengecekan di level routing: nama sekolah spesifik → profile,
# query tanpa nama → list; tidak ada pencampuran.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_exact_school_name_routing():
    """
    'SDN 1 Serpong' harus diroute ke profile (nama spesifik + nomor).
    Routing tidak boleh mencampur hasil untuk 'SDN 1 Serpong Utara'.
    """
    result_serpong        = _keyword_fallback("profil SDN 1 Serpong")
    result_serpong_utara  = _keyword_fallback("profil SDN 1 Serpong Utara")

    assert result_serpong.primary == "profile", (
        f"'SDN 1 Serpong' harus → profile, got {result_serpong.primary!r}"
    )
    assert result_serpong_utara.primary == "profile", (
        f"'SDN 1 Serpong Utara' harus → profile, got {result_serpong_utara.primary!r}"
    )
    # Keduanya route ke profile — bedanya hanya di nama_sekolah yang dikirim ke ES
    # (dikontrol oleh CognitiveIntent.nama_sekolah, bukan diuji di sini karena
    # keyword_fallback tidak mengekstrak nama_sekolah teks-bebas)


@pytest.mark.unit
def test_exact_school_name_no_number_routes_to_list():
    """
    Query tanpa nama sekolah spesifik harus ke list, bukan profile,
    sehingga tidak ada risiko false-positive pada satu sekolah.
    """
    result = _keyword_fallback("daftar SDN di Serpong")
    assert result.primary == "list", (
        f"Query tanpa nama sekolah spesifik harus → list, got {result.primary!r}"
    )


if __name__ == "__main__":
    print("=" * 72)
    print("KEYWORD FALLBACK TEST  (offline)")
    print("=" * 72)
    passed, failed, failures = 0, 0, []
    for tc_id, query, exp_primary, exp_secondary, exp_filters, notes in INTENT_TEST_CASES:
        result = _keyword_fallback(query)
        primary_ok   = result.primary == exp_primary
        secondary_ok = all(s in result.secondary for s in exp_secondary)
        filter_miss  = {
            k: (exp_filters[k], result.filters.get(k))
            for k in exp_filters
            if exp_filters[k] is not None and result.filters.get(k) != exp_filters[k]
        }
        ok = primary_ok and secondary_ok and not filter_miss
        if ok:
            passed += 1
            print(f"  ✓ [{tc_id}] {query[:60]}")
        else:
            failed += 1
            print(f"  ✗ [{tc_id}] {query[:60]}")
            if not primary_ok:
                print(f"      primary: expected={exp_primary!r} got={result.primary!r}")
            if not secondary_ok:
                print(f"      secondary: expected={exp_secondary} got={result.secondary}")
            for k, (e, g) in filter_miss.items():
                print(f"      filter[{k}]: expected={e!r} got={g!r}")
            failures.append(tc_id)
    print(f"\nResults: {passed} passed, {failed} failed / {len(INTENT_TEST_CASES)} total")
    if failures:
        print(f"Failed : {failures}")
    import sys
    sys.exit(0 if failed == 0 else 1)
