# CHANGELOG — Sesi Optimasi RAG Backend
**Tanggal:** 2026-04-21
**Scope:** Audit dan perbaikan performance, error handling, security, dan maintainability

---

## Prioritas 1 — Critical Security

| ID | File | Perubahan |
|----|------|-----------|
| SEC-001 | `.env.production` | **SKIP** — akan dihandle dengan proper secret management saat push ke GitLab |

---

## Prioritas 2 — High (Bug Fungsional + Security)

| ID | File | Perubahan |
|----|------|-----------|
| PERF-001 | `app/services/query_handlers/aggregation_handler.py` | Fix bug: query nasional hardcoded ke index `"sekolah"` → baca `ELASTICSEARCH_INDEX` dari env (~3 baris) |
| SEC-003 | `app/main.py`, `.env.example` | CORS wildcard `"*"` → baca `ALLOWED_ORIGINS` dari env, fallback `localhost:3000`; placeholder ditambah ke `.env.example` (~9 baris) |
| SEC-004 | `app/main.py` | Hapus teks query user dari log output `[RouterV2]` (~2 baris) |

---

## Prioritas 3 — High (Error Handling)

| ID | File | Perubahan |
|----|------|-----------|
| ERR-003 | `app/utils/cache.py` | `save_chat_history`: tambah null-check `redis_client` sebelum `.pipeline()` (+2 baris) |
| ERR-005 | `app/utils/cache.py` | `get_chat_history` dan `get_counter`: tambah null-check `redis_client` (+4 baris) |
| ERR-002 | `app/background/analytics.py` | `run_analytics_bulk`: bungkus `client.bulk()` dengan try/except, return `{failed: N}` jika ES down (+4 baris) |
| ERR-001 | `app/services/core/aggregation_core.py` | `get_entity_doc_count`: bungkus ES search dengan try/except, return `0` jika gagal (+4 baris) |
| ERR-004 | `app/background/low_score_handler.py` | `asyncio.create_task` rewrite disimpan ke `_background_tasks` set agar tidak di-GC sebelum selesai (+3 baris) |

---

## Prioritas 4 — Medium (Error Handling + Performance)

| ID | File | Perubahan |
|----|------|-----------|
| ERR-006 | `app/background/analytics.py` | Fix bug indentasi: blok `try/except` dipindah masuk ke dalam `async with _es_lock` — tutup race condition double-checked locking (~20 baris re-indent) |
| ERR-008 | `app/main.py` | Startup failure: `logger.warning` → `logger.error` + `raise RuntimeError` — server tidak jalan setengah siap (+2 baris) |
| PERF-007 | `app/services/location_service.py` | `size: 20000` diganti `_FIELD_TERM_SIZE` per field (`desa_kelurahan` → 90.000); tambah warning otomatis jika limit tersentuh (+12 baris) |
| SEC-006 | `app/main.py` | `AskRequest.question`: tambah `Field(min_length=1, max_length=500)` dan `field_validator` strip whitespace (+9 baris) |
| ERR-007 | `app/utils/rag_evaluation_logger.py` | Dua `bare except: pass` → `except Exception as e` + `logger.debug` (+4 baris) |

---

## Prioritas 5 — Medium (Performance + Maintainability)

| ID | File | Perubahan |
|----|------|-----------|
| PERF-005 | `app/router.py` | LangGraph `_COMPILED_GRAPH` singleton — compile sekali saat module load, bukan per-request (+13 baris, -9 baris) |
| DUP-003 | `app/services/core/jenjang_constants.py` *(file baru)* | `JENJANG_MAP` + `JENJANG_KEYWORDS` — single source of truth untuk mapping jenjang pendidikan (25 baris) |
| DUP-003 | `app/services/query_handlers/aggregation_handler.py` | Hapus `_JENJANG_KEYWORDS` + `_JENJANG_PENGGANTI`, import dari `jenjang_constants` (-12 baris, +2 baris) |
| DUP-003 | `app/services/query_handlers/list_school_handler.py` | Hapus local `_JENJANG_MAP`, import dari `jenjang_constants` (-11 baris, +2 baris) |
| DUP-003 | `app/main.py` | Hapus `_JENJANG_MAP_GEO`, import dari `jenjang_constants` (-9 baris, +2 baris) |
| DUP-004 | `app/services/entity_service.py` | `_strip_loc_prefix` → public `strip_loc_prefix`; ganti 3 inline regex dengan pemanggilan fungsi (+0 baris net, regex lama tidak handle `adm.`) |
| DUP-004 | `app/services/query_handlers/list_school_handler.py` | Import `strip_loc_prefix`, ganti 2 inline regex (+1 baris, -3 baris) |
| SEC-008 | `app/background/low_score_handler.py` | `hash(query)` → `hashlib.md5(...).hexdigest()` — deterministik lintas restart (+2 baris) |
| SEC-008 | `app/background/rewrite_worker.py` | `hash(q)` → `hashlib.md5(...).hexdigest()` (+2 baris) |
| PERF-009 | `app/utils/cache.py` | Health check: O(N) SCAN → O(1) `INFO keyspace`; tambah null-check Redis (-7 baris, +5 baris) |
| ERR-009 | `app/services/search/search_service.py` | `fuzzy_search_all_locations`: bungkus ES search dengan try/except, return `{}` jika gagal (+4 baris) |
| ERR-010 | `app/main.py` | Tiga endpoint `/report/*` dilindungi `X-Report-Key` header via `REPORT_API_KEY` env var (+12 baris) |
| DUP-005 | `app/services/core/result_model.py` | Tambah `build_ambiguous_message()` helper (+9 baris) |
| DUP-005 | `app/main.py` | Ganti inline candidates-to-message logic dengan `build_ambiguous_message()` (-7 baris, +1 baris) |
| DUP-005 | `app/services/query_handlers/aggregation_handler.py` | Ganti inline logic dengan `build_ambiguous_message()` (-8 baris, +1 baris) |

---

## Item Tambahan (Teridentifikasi Saat Verifikasi)

| ID | File | Perubahan |
|----|------|-----------|
| PERF-008 | `app/main.py` | `get_embedding_model()` → `await asyncio.to_thread(get_embedding_model)` — tidak blokir event loop saat startup (+1 baris) |
| PERF-010 | `app/main.py` | `build_reference_locations` di-warm saat startup via `asyncio.to_thread` — bukan first-call mid-request (+2 baris) |
| SEC-002 | `.env` | Hapus duplikasi entry `REDIS_URL` di baris 15 (-1 baris) |

---

## Ringkasan File yang Diubah

| File | Status | Estimasi Baris Berubah |
|------|--------|------------------------|
| `app/main.py` | Diubah | ~40 baris |
| `app/services/query_handlers/aggregation_handler.py` | Diubah | ~22 baris |
| `app/services/query_handlers/list_school_handler.py` | Diubah | ~16 baris |
| `app/services/core/aggregation_core.py` | Diubah | ~5 baris |
| `app/services/core/result_model.py` | Diubah | ~9 baris |
| `app/services/core/jenjang_constants.py` | **File Baru** | 25 baris |
| `app/router.py` | Diubah | ~22 baris |
| `app/services/entity_service.py` | Diubah | ~10 baris |
| `app/services/location_service.py` | Diubah | ~14 baris |
| `app/services/search/search_service.py` | Diubah | ~5 baris |
| `app/utils/cache.py` | Diubah | ~13 baris |
| `app/utils/rag_evaluation_logger.py` | Diubah | ~4 baris |
| `app/background/analytics.py` | Diubah | ~24 baris |
| `app/background/low_score_handler.py` | Diubah | ~6 baris |
| `app/background/rewrite_worker.py` | Diubah | ~3 baris |
| `.env.example` | Diubah | ~3 baris |
| `.env` | Diubah | ~1 baris |

**Total: 16 file diubah + 1 file baru + 1 CHANGELOG.md**

---

## Outstanding — Belum Diselesaikan

| Item | Keterangan |
|------|------------|
| `SEC-001` | Credential di `.env.production` — ditangani saat push ke GitLab dengan proper secret management |

---

## Resolved Post-CHANGELOG (2026-04-21)

| Item | File | Perubahan |
|------|------|-----------|
| CRITICAL | `app/services/core/es_query_builder.py` | `build_qc_filter()`: hapus nested wrapper — `qc` adalah plain object, bukan nested. Ganti ke direct `term` filters (`qc.qc_1`…`qc_13`). |
| CRITICAL | `app/services/core/aggregation_core.py` | `build_qc_aggs()`: hapus nested agg wrapper — spread `kolom_aggs` langsung sebagai top-level aggs. Update docstring. |
| HIGH | `app/services/query_handlers/comparison_handler.py` | Hapus agregasi `total_siswa`/`avg_siswa`/`max_siswa` (field tidak ada di index). Hapus downstream reads + 3 key palsu di return dict. Fix index hardcoded → `os.getenv("ELASTICSEARCH_INDEX")`. |
| HIGH | `app/services/narrator_service.py` | `_prompt_school_vs_school`: ganti `siswa_total`/`guru_total` dengan `"data tidak tersedia"`. `_build_school_table`: kolom siswa hardcode `'-'`. `_prompt_school_profile`: ganti baris Guru/Siswa di prompt dengan `"data tidak tersedia di indeks ini"`. Hapus catatan kritis `guru_total == 0` (field memang tidak ada di index, bukan data belum dilaporkan). |
| `.env.example` | `.env.example` | Tambah section `# Security` + `REPORT_API_KEY=your-secret-report-key-here`. |
| `.gitignore` | `.gitignore` | Tambah `.env.development`, `*.env`, `scripts/es-export/`, `scripts/checkpoint_qc.json`. |
