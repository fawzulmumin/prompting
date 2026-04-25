## Priority 1: Dependency-Aware Execution — SELESAI (April 2026)

**File yang diubah:**
- `app/services/core/cognitive_router.py` — tambah field `dependencies` di `CognitiveIntent`, update `_keyword_fallback()` untuk detect ranking+list pattern, update `_parse_llm_output()`
- `app/services/core/multi_intent_orchestrator.py` — ditulis dari scratch, 2-tier execution
- `tests/test_dependency_execution.py` — 6 unit test baru
- `tests/test_multi_module.py` — fix 1 broken test (patch path), skip 4 pre-existing failures

**Pattern dependencies:**
```json
{"list": {"depends_on": "aggregation", "inject_from_result": {"provinsi": "top_location"}}}
```

**Trigger keyword fallback:**
ranking word (`terbanyak`/`tertinggi`/`terbesar`) + list word (`tampilkan`/`daftar`/`list`/`sekolahnya`)

**Known limitation:**
- `inject_from_result` hanya support `"provinsi"` → `"top_location"` untuk saat ini
- 4 test di-skip karena fitur belum ada: `_latest_semester_id`, `sarana_prasarana` di `build_profile()`

**Test command:**
```
pytest tests/ -v -m unit --ignore=tests/test_es_health.py --ignore=tests/test_redis.py
```

---

## Priority 3: Comparison & List Handler Consume Intent — SELESAI (April 2026)

**File yang diubah:**
- `app/services/query_handlers/comparison_handler.py` — tambah `_extract_wilayah_from_intent()`,
  `handle_comparison_query()` sekarang trust `intent.wilayah` jika tersedia, fallback ke
  `_resolve_vs_query()` jika kosong
- `app/services/query_handlers/list_school_handler.py` — tambah `_inject_intent_wilayah()`,
  `handle()` inject wilayah dari intent ke `parsed` jika `parsed["wilayah"]` masih kosong
- `app/services/core/multi_intent_orchestrator.py` — `_call_module()` + `_dispatch_single()`
  sekarang pass `intent` dan `filters` ke comparison handler
- `tests/test_dependency_execution.py` — tambah 2 test baru (total 8 unit test)

**Pattern yang digunakan:**
Trust intent → skip resolver, intent kosong → fallback ke parsing lama
(konsisten dengan `aggregation_handler.py`)

**Test command:**
```
pytest tests/ -v -m unit --ignore=tests/test_es_health.py --ignore=tests/test_redis.py
```
Result: 80 passed, 4 skipped, 0 failed

---

## Priority 4: Observability & Production Readiness — SELESAI (April 2026)

### 4A: Structured Logging + Cache Key Normalization

- `app/utils/cache.py` — `_log()` helper emit single-line JSON `{"event":..., "ts":..., ...}`;
  `_normalize_cache_key()` lowercase + strip + punct removal dengan MD5 passthrough (len==32 + all-hex)
- Semua `logger.info/warning/error` di `cache.py` diganti ke structured JSON format
- Key normalization diterapkan di `get_cache()` dan `set_cache()` sebelum operasi Redis;
  log hanya jika original ≠ normalized (no spam)

### 4B: Rate Limiting DeepSeek

- `app/providers/deepseek.py` — `_check_rate_limit()` (50 req/60s, env-tunable via
  `DEEPSEEK_RL_MAX_REQ`), circuit breaker `_check_circuit_breaker()` (5 failures → open
  120s, env-tunable via `DEEPSEEK_CB_THRESHOLD`)
- Rate limit check di **luar** retry loop (`call_deepseek` = wrapper, `_call_deepseek_inner`
  = retry-decorated) — retries tidak double-count
- Fail open jika Redis tidak available (graceful degradasi)
- `_record_cb_failure()` dipanggil pada status 429/402; `_reset_cb()` dipanggil pada sukses
- `app/utils/cache.py` — `delete_cache()` sudah ada sejak awal (dipakai oleh `_reset_cb`)

### 4C: Hallucination Guardrails

- `app/services/narrator_service.py`:
  - **Fix bug**: validasi angka di `narrate_school_profile` dipindah ke posisi yang benar —
    sebelumnya ada di dalam `if not narasi:` sehingga tidak pernah dieksekusi (dead code)
  - `_contains_hallucination()` — 5 regex pattern, case-insensitive; AI self-reference +
    competitor model names; dipakai di `_call_narrator()` setelah LLM response
  - `_extract_numbers_from_text()` — handle format Indonesia (`1.234`), filter nilai `< 10`
    (abaikan nomor urut), deduplicated
  - `_validate_numbers_in_narasi()` — diubah return type `bool` → `Optional[str]` (backward-
    compatible: semua caller tetap pakai `if not validate(...)`); kini terima dict **atau** list
  - `narrate_multi_module()` — validasi angka dari `merged_context` ada di output gabungan LLM;
    fallback ke `merged_context` jika ada yang hilang
  - `_call_narrator()` — hallucination check setelah response, return `fallback` ke caller

### Test Coverage Final

- `tests/test_dependency_execution.py`: **14 unit test** (semua Priority 1–4)
- Total test suite: **90 passed, 4 skipped, 0 failed**

```
pytest tests/ -v -m unit --ignore=tests/test_es_health.py --ignore=tests/test_redis.py
```

### Yang masih di-skip (pre-existing, bukan regresi kita)

- `test_es_query_contains_semester_filter` — `_latest_semester_id` belum diimplementasi
- `test_profile_builder_jamban_*` (3 test) — `sarana_prasarana` key di `build_profile()` belum diimplementasi

---

## Pre-GitLab Checklist

- [ ] **Bump cache version prefix** `v3` → `v4` di `cognitive_router.py` (breaking change:
  field `dependencies` baru di `CognitiveIntent` tidak backward-compatible dengan cache lama)
- [ ] **Set environment variables** di `.env.production`:
  ```
  DEEPSEEK_RL_MAX_REQ=50
  DEEPSEEK_CB_THRESHOLD=5
  ```
- [ ] **Info ke tim vektorisasi**: field `content_vector` di index `idx_sekolah_qc` belum
  di-populate — hybrid search (Priority 2) masih blocked sampai populate selesai
- [ ] **Run integration test** sebelum push:
  ```
  RUN_INTEGRATION=1 pytest tests/ -v -m integration
  ```
- [ ] **Circular import aman**: orchestrator import handler via lazy import di `_dispatch_module`,
  bukan top-level — sudah diverifikasi tidak ada circular dependency
