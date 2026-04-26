# Changelog — QC Feature Fix & Code Cleanup
**Tanggal:** 2026-04-26
**Status:** Production-ready — 155 passed, 0 failed, 0 warnings

---

## Bug Kritis (P0)

### Fix: ELASTICSEARCH_QC_PERCENTAGE tidak ditangani di main.py
- **Gejala:** Query "berapa persen sekolah datanya bersih?" → "Tidak ditemukan satuan pendidikan di Tidak diketahui"
- **Root cause:** main.py tidak punya branch untuk provider ELASTICSEARCH_QC_PERCENTAGE
- **Fix:** Tambah import narrate_qc_percentage + branch handler di blok AGGREGATION
- **File:** app/main.py

### Fix: JENJANG_MAP NameError (latent crash) — ditemukan oleh ruff
- **Gejala:** Potensi NameError di production saat list handler memproses jenjang
- **Root cause:** Import JENJANG_MAP di-remove karena dikira unused, padahal _JENJANG_MAP (dengan underscore) yang tidak ada
- **Fix:** Restore import JENJANG_MAP + rename _JENJANG_MAP → JENJANG_MAP di seluruh handler
- **File:** app/services/query_handlers/list_school_handler.py

---

## Perbaikan Output (P1)

### Fix: Narasi profil tidak menyebut masalah QC
- **Gejala:** Query "apa masalah data RA Baitul Huda" → narasi LLM hanya bahas identitas/siswa/SDM/sarana
- **Root cause:** _prompt_school_profile() tidak include kualitas_data dalam prompt LLM
- **Fix:**
  - Ekstrak kualitas_data, map ke nama readable, inject ke DATA SEKOLAH
  - Heading "## Kualitas & Kelengkapan Data (WAJIB — ada N masalah)" di-inject kondisional dari Python hanya saat total_masalah > 0
  - Jumlah paragraf dinamis: n_paragraf = "5" jika ada masalah, "4" jika bersih
  - Daftar masalah konkret langsung di instruksi (bukan referensi abstrak)
- **File:** app/services/narrator_service.py

### Fix: Narasi QC list tidak muncul meskipun filter aktif
- **Gejala:** Query koordinat kosong → tabel muncul tapi narasi tidak sebut masalah QC
- **Root cause:** 3 gap pipeline:
  1. qc_field_list tidak dipropagasi ke parsed di list_school_handler
  2. _active_filters di main.py tidak forward qc_masalah_only/qc_field_list/qc_field
  3. _qc_mode hanya cek qc_masalah_only, tidak cek qc_field_list/qc_field
- **Fix:** Propagate qc_field_list di list_school_handler; forward di main.py; expand deteksi _qc_mode
- **File:** list_school_handler.py, main.py, narrator_service.py

### Enhancement: Adaptif output QC list saat hasil ≤ 10 sekolah
- **Before:** Selalu "contoh 2-3 sekolah"
- **After:** ≤ 10 → "sebutkan SEMUA beserta detail masalah per sekolah"; > 10 → "3-5 contoh + pola umum"
- **File:** app/services/narrator_service.py

---

## Code Quality (P2) — ditemukan oleh ruff

### Auto-fixed
- 366 trailing whitespace (W293/W291) — di-fix bulk
- f-string tanpa placeholder → stripped f prefix (F541)
- Ambiguous variable name l → ln (E741)
- Two statements on one line → split (E702)

### Manual fixed
- F401 app.config unused import di main.py → removed
- F841 dead variables di _prompt_school_profile (guru_pns, guru_honor, rasio_str, rasio_konteks, pct_serti — sisa prompt lama) → removed

---

## Test & Infrastructure

### Fix: PVG_04 event loop closed di test ke-39
- **Fix:** asyncio_default_fixture_loop_scope = function di pytest.ini + function-scoped event_loop fixture
- **File:** pytest.ini, tests/conftest.py

### Fix: DeprecationWarning rc.close() → rc.aclose()
- **File:** tests/test_redis.py

### Test baru (8 total, semua di tests/test_qc_multi_field.py)
| Test | Yang dijaga |
|------|-------------|
| test_qc_percentage_full_response_no_error_strings | End-to-end: provider=QC_PERCENTAGE, tidak ada "Tidak ditemukan" |
| test_prompt_school_profile_includes_qc_data | _prompt_school_profile inject kualitas_data ke prompt |
| test_prompt_school_profile_no_qc_section_when_clean | Heading WAJIB tidak muncul saat data bersih (total_masalah=0) |
| test_build_qc_summary_context_nonempty_when_masalah | Non-empty QC summary saat schools punya masalah |
| test_build_qc_summary_context_empty_when_no_qc_data | Empty string saat tidak ada QC data |
| test_qc_mode_detected_from_qc_field_list | _qc_mode trigger dari qc_field_list dan qc_field |
| test_jenjang_map_import_accessible | JENJANG_MAP dapat diakses tanpa NameError |
| test_redis_aclose | rc.aclose() tidak raise DeprecationWarning |

---

## Files Changed
| File | Perubahan |
|------|-----------|
| app/main.py | ELASTICSEARCH_QC_PERCENTAGE handler; qc fields di _active_filters; remove unused import |
| app/services/narrator_service.py | QC injection di profil; adaptif QC list; expand _qc_mode; cleanup dead variables |
| app/services/query_handlers/list_school_handler.py | Propagate qc_field_list; fix JENJANG_MAP NameError |
| pytest.ini | asyncio_default_fixture_loop_scope = function |
| tests/conftest.py | Function-scoped event_loop fixture |
| tests/test_qc_multi_field.py | 8 test baru |
| tests/test_redis.py | rc.close() → rc.aclose() |
