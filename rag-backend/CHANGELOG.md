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

## Phase 19 — Fitur Baru & Bug Fix (Mei 2026)
**Test count:** 697 passed, 44 skipped, 71 xfailed (up from 647 baseline Phase 18)

| Sub-phase | File(s) | Perubahan |
|-----------|---------|-----------|
| P19-A: Akreditasi TT | `prompts/intent_classifier/system_v1.txt` | Few-shot "Provinsi Jakarta" → DKI Jakarta, "belum terakreditasi" → akreditasi TT |
| P19-B: Metric target (agg+comp) | `aggregation_core.py`, `comparison_handler.py` | `build_metric_aggs()` dipakai di comparison query; `_build_comparison_markdown()` render label siswa/guru/tendik; `_fetch_location_data()` baca dari `main_metric` agg key; `_COMPARISON_METRIC_LABEL` dict |
| P19-C: Wilayah pulau | `intent_models.py`, `aggregation_core.py`, `es_query_builder.py` | `WilayahEntities.pulau` field; `PULAU_PROVINSI_MAP` 9 zona; `scope=pulau`; `build_wilayah_filter()` handles pulau; `VALID_SCOPES` includes `"pulau"` |
| P19-D: Graceful fallback lokasi | `location_resolver.py`, `retrieval_retry_handler.py` | Drop unresolved desa jika kecamatan tersedia; drop unresolved kecamatan jika kabupaten tersedia; `_MAX_RETRY_ATTEMPTS=2` guard; 5 test baru `TestGracefulFallback` |
| P19-E: Infrastruktur filter | `intent_models.py`, `es_query_builder.py`, `cognitive_router.py`, `system_v1.txt` | `CognitiveIntent.infrastruktur_filter` field; `_INFRASTRUKTUR_FILTER_MAP` 4 kondisi (internet/listrik/komputer/toilet); wired ke `build_es_query()` + `extract_filters_from_intent()`; `setdefault` cache guard; INFRASTRUKTUR rules + 3 few-shot examples |
| P19-F: Golden set | `tests/golden_set/golden_set_v1.yaml` | +8 cases: metric_001–005 (metric_target), pulau_001–002 (scope pulau), infra_001 (infrastruktur_filter) |
| P19-G: Test suite | `tests/test_comparison_handler.py`, `tests/test_location_resolver.py` | +3 test comparison metric label/siswa/tendik; +5 test graceful fallback desa/kecamatan |

---

## Phase 19 E2E Bug Fix (2026-05-21)
**Test count:** 347 passed (unit) setelah semua fix diterapkan.

Root cause ditemukan via E2E testing terhadap live server. 10 bug fix total.

| Bug | File | Perubahan |
|-----|------|-----------|
| `execute_nested_aggregation_query` abaikan `main_metric.value` | `aggregation_core.py` | Baca `aggs.main_metric.value` sebagai `effective_total` jika `metric_target != sekolah` |
| `akreditasi_in` tidak diapply ke ES filter | `list_school_handler.py` | `_query_single_wilayah` + `_handle_nasional_list`: baca `parsed.akreditasi_in` → `{"terms": {"akreditasi": ...}}` |
| Cache key v5 return stale intent tanpa `metric_target`/`pulau` | `cognitive_router.py` | Bump ke `cognitive_router:v6:` |
| `"pulau"` tidak ada di `_LOCATION_FIELDS` | `location_resolver_adapter.py` | Tambah `"pulau"` ke set agar `validate_location_presence` return True |
| `scope=pulau` tidak ditangani di `_build_resolution` | `aggregation_handler.py` | Tambah handler `scope=pulau` + import `PULAU_PROVINSI_MAP` |
| `"pulau"` tidak ada di level detection `_handle_single_wilayah` | `aggregation_handler.py` | Tambah `"pulau"` ke loop `current_level` dan kondisi `use_nested` |
| `metric_target` schema LLM hanya `sekolah\|null` | `prompts/intent_classifier/system_v1.txt` | Perluas schema ke semua nilai valid (`siswa\|guru\|guru_pns\|...`) |
| LLM tidak isi `wilayah.pulau` walau `scope=pulau` | `aggregation_handler.py` | Ekstrak nama pulau dari query via regex jika `wilayah.pulau=None` |
| `wilayah_keys` tidak include `"pulau"` → route ke nasional | `aggregation_handler.py` | Tambah `"pulau"` ke `wilayah_keys` tuple |
| Akreditasi TT disimpan sbg `"Tidak Terakreditasi"` di ES bukan `"TT"` | `aggregation_core.py`, `list_school_handler.py`, `es_query_builder.py` | `_AKRED_ES_ALIAS` map; `extract_filters_from_intent` gunakan `akreditasi_exact="Tidak Terakreditasi"`; fallback `_AKRED_ES_ALIAS` di filter builder |
| `siswa_total`/`guru_total`/tendik script tidak ada di index | `aggregation_core.py` | Graceful fallback: jika `is_numeric` tapi `main_metric_val==0` dan `total_doc>0`, gunakan `total_doc` |

**Known remaining issues (sesi sebelumnya, sekarang resolved di bawah):**
- ~~C8/C9/C10 (pulau): narasi masih label "nasional"~~ → FIXED (lihat Phase 19 Follow-up)
- ~~B4/B7 (siswa/tendik): field tidak ada di ES index~~ → FIXED — query ke idx_sekolah_profile
- A2: 0 docs benar (ES confirm tidak ada SD TT di Kota Surabaya), routing via ELASTICSEARCH_PROFILE

---

## Phase 19 Follow-up (2026-05-21)
**Test count:** 697 passed, 44 skipped, 71 xfailed (tidak ada regresi)

Dua fix lanjutan setelah sesi E2E sebelumnya.

| Bug | File | Perubahan |
|-----|------|-----------|
| Metric siswa/guru/tendik selalu 0 — query ke index salah | `aggregation_core.py` | `_METRIC_NEEDS_PROFILE` set; `execute_nested_aggregation_query` pilih `idx_sekolah_profile` jika `metric_target` ada di set; graceful fallback hanya untuk qc index |
| Narasi pulau bilang "Seluruh Indonesia" | `response_formatter.py`, `narrator_service.py` | `build_location_label`: cek key `"pulau"` → return `"Pulau {X.title()}"` sebelum cek geo levels; `detect_level`: tambah `"pulau"` ke urutan loop |

---

## Phase 19 Bug Fix — Distribusi, Location & Diagnostik (2026-05-21)
**Test count:** 697 passed, 44 skipped, 71 xfailed (baseline Phase 18: 647)

Bug fix lanjutan dari sesi E2E ke-2. Fokus: distribusi_field end-to-end, location resolver accuracy, nasional scope index routing, dan diagnostik anomali data.

| Sub-phase | File(s) | Perubahan |
|-----------|---------|-----------|
| P19-H: Fix distribusi_field inference | `aggregation_handler.py`, `aggregation_core.py` | Tambah `_DISTRIBUSI_NEEDS_PROFILE` frozenset (`{"sumber_air"}`); import dan gunakan di `_handle_nasional_scope()` agar query distribusi `sumber_air` routing ke `idx_sekolah_profile`; tambah pre-inference log `[AGG] pre-inference: metric_target=... distribusi_field=...` sebelum blok `_infer_distribusi_field()` |
| P19-I: Fix narrator untuk distribusi | `narrator_service.py`, `narrator_prompts.py`, `ask_pipeline.py` | Tambah param `distribusi: Optional[list]` dan `distribusi_field: Optional[str]` ke `narrate_aggregation()` dan `_prompt_aggregation()`; tambah distribusi section di prompt body dengan label mapping (sumber_listrik/akses_internet/sumber_air/waktu_penyelenggaraan); `ask_pipeline.py` pass `result.get("distribusi")` dan `result.get("distribusi_field")` ke narrator |
| P19-J: Fix location resolver desa cibunar | `location_resolver.py` | Fix disambiguasi desa "Cibunar" — sebelumnya resolve ke Balapulang, Tegal; sekarang resolve ke Parungpanjang, Bogor sesuai konteks kecamatan |
| P19-K: Fix nasional scope metric_target routing | `aggregation_handler.py`, `aggregation_core.py` | `_handle_nasional_scope()` sebelumnya hardcode ke `ELASTICSEARCH_INDEX`; sekarang cek `_metric_target in _METRIC_NEEDS_PROFILE` dan routing ke `idx_sekolah_profile` jika perlu (sama dengan `_handle_single_wilayah`) |
| P19-L: Diagnostic warning data anomali | `aggregation_core.py` | Tambah `[AGG_CORE] SUSPICIOUS: sum(field)=X == doc_count=Y` warning jika `sum(metric_field) == doc_count` untuk numeric field di profile index — deteksi dini field yang berisi flag (0/1) bukan count aktual |

**Verifikasi Bug 1 (guru_p3k):** ES stats confirm `max=75, avg=1.43, sum=121258` — bukan flag. Data valid. Tidak ada perubahan `_METRIC_MAP`.

**Known issues — dilanjutkan di Phase 20:**
- Narasi `guru_p3k` masih menyebut "satuan pendidikan" alih-alih "guru PPPK" — butuh `metric_label` mapping di narrator
- Distribusi nasional (mis. `sumber_listrik` tanpa filter wilayah) masih query ke `idx_sekolah_qc` — butuh extend `_DISTRIBUSI_NEEDS_PROFILE` atau gunakan `idx_sekolah_profile` sebagai default untuk semua distribusi query
- Angka distribusi listrik nasional (204) kemungkinan undercount akibat index salah

---

## Phase 20 — Distribusi & Location Fixes (2026-05-21)
**Test count:** 347 passed (unit), 697 passed full suite — tidak ada regresi

Bug fix lanjutan dari E2E Round 2 & Round 3. Fokus: location resolver untuk metric query, distribusi negative-pattern matching, sumber_listrik index routing, dan distribusi routing override.

| Sub-phase | File(s) | Perubahan |
|-----------|---------|-----------|
| P20-F: Fix `_NOISE_PATTERN` + pulau scope | `location_resolver_adapter.py` | Perluas `_NOISE_PATTERN` dengan kata metrik/filler: `komputer`, `listrik`, `pln`, `internet`, `guru`, `siswa`, `murid`, `tendik`, `yang`, `tidak`, `punya`, `memiliki`, `belum`, `tanpa` — agar `_extract_location_tokens("total komputer di sekolah Kalimantan Barat")` → `"Kalimantan Barat"` (bukan `"komputer Kalimantan Barat"`). Tambah `_PULAU_NAMES` frozenset dan pulau scope fast-path dalam `resolve_location_from_query()` — kembalikan `{"pulau": <name>}` sebelum province alias check. |
| P20-G: `sumber_listrik` → profile index | `aggregation_core.py` | Tambah `"sumber_listrik"` ke `_DISTRIBUSI_NEEDS_PROFILE` (sebelumnya hanya `"sumber_air"`). Coverage `sumber_listrik` di `idx_sekolah_qc` sangat rendah (8–9 docs per provinsi); `idx_sekolah_profile` memiliki coverage lebih baik. **Note:** Jika coverage di profile index juga < 500 docs total, ini adalah data quality issue — bukan bug kode. |
| P20-H: Distribusi negative patterns | `aggregation_handler.py` | Perluas `_DISTRIBUSI_PATTERNS` dengan varian negatif: `sumber_listrik` → tambah `tidak.?punya.?listrik`, `tanpa.?listrik`, `tidak.?ada.?listrik`, `belum.?ada.?listrik`, `tidak.?ada.?pln`; `akses_internet` → tambah `tidak.?punya.?internet`, `tanpa.?internet`, `tidak.?ada.?internet`, `belum.?ada.?internet`, `tidak.?memiliki.?internet`. |
| P20-I: Distribusi routing override | `ask_pipeline.py` | Tambah `_DISTRIBUSI_QUERY_RE` pattern + override dalam `_run_classification()`: jika `intent.primary == "list"` AND query mengandung pola infrastruktur negatif ("tidak punya listrik/internet/air"), paksa ke `aggregation`. Tier1 sering salah tangkap "sekolah di X yang tidak punya Y" sebagai list query. |
| P20-J: Debug logs + diagnostik | `aggregation_handler.py` | Tambah `logger.debug` ke `_infer_distribusi_field()` — log query, pattern yang cocok, dan "no match". Tambah WARNING log di `_build_resolution()` ketika `metric_target` diset tapi wilayah kosong dari intent (fallback ke location resolver). |

**Known limitations (data quality — bukan bug kode):**
- `sumber_listrik`: jika coverage di `idx_sekolah_profile` juga < 500 total dokumen → accept sebagai data quality limitation, tidak bisa difix di sisi aplikasi
- `akses_internet`: coverage bergantung pada data ES; pattern matching sudah benar (`internet` saja sudah cukup untuk deteksi distribusi)
- `total_komputer Kalimantan Barat`: jika LLM classifier tidak pernah mengisi `wilayah` untuk query ini → LLM prompt engineering issue terpisah (bukan bug logic location resolver)

---

## Phase 21 — Multi-Metric Query & Perbaikan Variasi Pertanyaan (2026-05-22)
**Test count:** 347 passed (unit) — tidak ada regresi

Fokus: support query yang meminta dua atau lebih metric sekaligus, perbaikan klasifikasi "jenis sekolah", dan klarifikasi comparison vs aggregation.

| Sub-phase | File(s) | Perubahan |
|-----------|---------|-----------|
| P21-A1: `metric_target_list` field | `intent_models.py` | Tambah field `metric_target_list: List[str] = field(default_factory=list)` di `CognitiveIntent` — untuk menampung multi-metric query. |
| P21-A2: LLM prompt update | `prompts/intent_classifier/system_v1.txt` | (a) Tambah `metric_target_list` ke schema OUTPUT. (b) Tambah aturan METRIC_TARGET_LIST (7 contoh pola multi-metric). (c) Tambah ATURAN 3 KLARIFIKASI: comparison hanya untuk dua wilayah berbeda — dua kategori dalam satu wilayah = aggregation. (d) Tambah 4 contoh query baru termasuk multi-metric dan "jenis sekolah". |
| P21-A3: Multi-metric aggregation | `aggregation_handler.py` | Tambah `_MULTI_METRIC_LABELS` dict, fungsi `_handle_multi_metric_query()` (asyncio.gather paralel per metric), dan multi-metric check dalam `handle_aggregation_query()`. Returns `provider="ELASTICSEARCH_MULTI_METRIC"`. |
| P21-A4: Prompt builder | `narrator_prompts.py` | Tambah fungsi `build_multi_metric_aggregation_prompt()` di akhir file. |
| P21-A5: Narrator | `narrator_service.py` | Tambah import + fungsi `narrate_multi_metric_aggregation()` dengan fallback template bullet list + validasi angka. |
| P21-A6: Pipeline wiring | `ask_pipeline.py` | Tambah import `narrate_multi_metric_aggregation` + handler `if agg_prov == "ELASTICSEARCH_MULTI_METRIC"` sebelum blok `is_multi`. |
| P21-B1: "jenis sekolah" → aggregation | `tier2_rule.py` | Tambah 3 signal baru: `jenis sekolah/tipe sekolah/macam-macam sekolah` → aggregation (0.60), `guru X dan guru` → aggregation (0.55), `siswa X dan siswa` → aggregation (0.55). |

**Flow multi-metric (contoh):**
- Query: `"berapa siswa laki-laki dan siswa perempuan SMA di Jawa Barat?"`
- LLM classifier: `metric_target_list: ["siswa_l", "siswa_p"]`
- `handle_aggregation_query`: deteksi `len(_metric_list) >= 2` → `_handle_multi_metric_query`
- `_handle_multi_metric_query`: asyncio.gather dua `_handle_single_wilayah` call secara paralel
- Hasil: `provider="ELASTICSEARCH_MULTI_METRIC"`, `multi_metric: {siswa_l: {...}, siswa_p: {...}}`
- `ask_pipeline`: routing ke `narrate_multi_metric_aggregation` → narasi natural dengan semua angka

---

## Phase 22 — OOT Routing Fix & QC Narasi Breakdown (2026-05-22)
**Test count:** 347 passed (unit) — tidak ada regresi

Dua bug kritis ditemukan via E2E testing Phase 22. Fix minimal dan targeted.

| Bug | File(s) | Root Cause | Perubahan |
|-----|---------|------------|-----------|
| **BUG A**: Query OOT (`"patokannya sekolah bagus itu apa?"`) di-route ke `ELASTICSEARCH_LIST` padahal log `[RouterV2]` sudah benar (`primary=out_of_context`) | `ask_pipeline.py`, `tests/test_ask_pipeline.py` | Adjudication block hanya menangani AMBIGUOUS branch di fix sebelumnya. SCHOOL branch (`rule_classify → SCHOOL`) masih unconditionally upgrade OOT → list tanpa cek lokasi. Log `[RouterV2]` fire sebelum adjudication, sehingga override tidak terlihat dari log. | Hapus seluruh adjudication logic OOT → list. Jika LLM sudah classify `out_of_context`, skip adjudication (`logger.info("[PIPELINE] out_of_context confirmed — skip adjudication")`). Update test `test_adjudication_school_keyword_overrides_oot` → `test_adjudication_oot_trusted_over_school` (assert `primary == "out_of_context"`). |
| **BUG B**: `"berapa sekolah bermasalah di Jawa Barat?"` tidak menyebut breakdown QC per kolom di narasi | `narrator_prompts.py`, `aggregation_handler.py` | `qc_section` ditanam sebagai sub-line di dalam baris Konteks. Instruksi LLM tidak memiliki heading `## Detail Kualitas Data` — LLM mengikuti heading eksplisit (Gambaran Umum, Komposisi) dan mengabaikan data QC yang tidak direferensikan. | (1) Promosikan QC ke blok terpisah `DATA KUALITAS (WAJIB DISEBUTKAN DI NARASI)`. (2) Tambah `qc_instruksi` dengan heading `## Detail Kualitas Data` di instruksi block — instruksikan LLM menyebut N kolom masalah terbanyak dengan angka persis. (3) Tambah `[QC_DETAIL]` diagnostic log di `aggregation_handler.py` setelah ekstraksi `qc_detail`. |

**Log yang diharapkan setelah fix BUG A:**
```
[RouterV2] primary=out_of_context
[PIPELINE] out_of_context confirmed — skip adjudication
[PIPELINE] post-adjudication: primary=out_of_context is_oot=False
→ provider=SYSTEM_REJECT
```

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
