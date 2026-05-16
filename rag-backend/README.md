# rag-backend

FastAPI backend untuk sistem RAG Kemendikdasmen. Bertanggung jawab atas klasifikasi intent, query ke Elasticsearch, dan narasi jawaban via LLM.

## Persyaratan

- Python 3.11+
- Elasticsearch 8.x (running)
- Redis 7.x (running)
- DeepSeek API key

## Setup

```bash
cd rag-backend

# Buat virtual environment
python -m venv .venv

# Aktifkan (Linux/Mac)
source .venv/bin/activate

# Aktifkan (Windows)
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Konfigurasi

Salin file contoh dan isi nilainya:

```bash
cp app/.env.example app/.env
```

| Variable | Keterangan |
|----------|------------|
| `ELASTICSEARCH_NODE` | URL Elasticsearch, contoh: `http://localhost:9200` |
| `ELASTICSEARCH_USERNAME` | Username ES |
| `ELASTICSEARCH_PASSWORD` | Password ES |
| `ELASTICSEARCH_INDEX` | Index utama, default: `idx_sekolah_qc` |
| `ELASTICSEARCH_PROFILE_INDEX` | Index profil, default: `idx_sekolah_profile` |
| `DEEPSEEK_API_KEY` | API key DeepSeek |
| `REDIS_URL` | URL Redis, contoh: `redis://localhost:6379` |
| `LOG_LEVEL` | Level logging: `DEBUG`/`INFO`/`WARNING`, default: `INFO` |
| `PORT` | Port server, default: `3000` |

## Menjalankan Server

```bash
# Development (auto-reload)
uvicorn app.main:app --reload --port 3000

# Production
uvicorn app.main:app --port 3000
```

## Menjalankan arq Worker

arq worker diperlukan untuk background jobs (cache, analytics, logging). Jalankan sebagai proses terpisah:

```bash
arq app.background.arq_worker.WorkerSettings
```

Di production, worker dijalankan sebagai sidecar container (lihat `k8s/deployment-backend.yaml`).

## Testing

```bash
# Regression guard — jalankan sebelum dan sesudah setiap perubahan
pytest tests/test_regression.py -v -m unit

# Full test suite (tidak butuh ES/Redis)
pytest tests/ -v -m unit --ignore=tests/test_es_health.py --ignore=tests/test_redis.py

# Hitung total test
pytest tests/ --ignore=tests/test_es_health.py --ignore=tests/test_redis.py -q

# Test spesifik
pytest tests/test_tier1_pattern.py tests/test_tier2_rule.py -v
pytest tests/test_list_school_handler.py -v -m unit
pytest tests/test_health_endpoint.py -v
```

**Status test saat ini: 642 passed**

## Struktur Aplikasi

```
app/
├── main.py                          # Entry point, startup/shutdown, /ask endpoint
├── config.py                        # Environment loading
├── router.py                        # LangGraph RAG fallback
├── api/
│   ├── models.py                    # AskRequest, AskResponse
│   ├── stream.py                    # POST /ask/stream — SSE endpoint
│   └── routes/
│       ├── health.py                # GET /health — status ES, Redis, cache, queue, LLM
│       └── reports.py
├── classification/
│   ├── intent_classifier.py         # Dispatcher Tier 1 → 2 → 3
│   ├── tier1_pattern.py             # Regex pattern, <5ms, 29 patterns
│   ├── tier2_rule.py                # Additive scoring, <20ms
│   └── tier3_llm.py                 # Placeholder (implementasi di cognitive_router)
├── services/
│   ├── core/
│   │   ├── ask_pipeline.py          # Pipeline utama, 10 langkah
│   │   ├── cognitive_router.py      # Tier 3 LLM + entity extraction
│   │   ├── es_query_builder.py      # Centralized Elasticsearch DSL builder
│   │   ├── location_resolver.py     # 5-step location resolution chain
│   │   ├── aggregation_core.py      # ES aggregation queries
│   │   ├── list_location_detector.py# 10 location detection helpers
│   │   └── multi_intent_orchestrator.py  # Parallel multi-intent dispatch
│   ├── query_handlers/
│   │   ├── list_school_handler.py   # Daftar sekolah per wilayah
│   │   ├── aggregation_handler.py   # Hitung & ranking sekolah
│   │   ├── comparison_handler.py    # Perbandingan antar wilayah
│   │   ├── profile_handler.py       # Profil satu sekolah
│   │   └── school_vs_handler.py     # Perbandingan dua sekolah spesifik
│   ├── narrator_service.py          # Narasi jawaban via LLM
│   ├── embedding_service.py         # MiniLM embedding + TieredCache L1+L2
│   └── formatters/
│       ├── narrator_prompts.py      # Prompt builder untuk narasi
│       └── response_formatter.py    # Format markdown response
├── background/
│   ├── arq_worker.py                # arq job definitions + WorkerSettings
│   ├── trigger.py                   # Dispatcher background jobs
│   ├── analytics.py                 # Log query ke ES analytics
│   ├── rewrite_worker.py            # Query rewrite via LLM
│   └── suggestion.py                # Generate related questions
├── providers/
│   └── deepseek.py                  # DeepSeek HTTP client + circuit breaker
└── utils/
    ├── tiered_cache.py              # L1 in-memory LRU + L2 Redis
    ├── cache.py                     # Redis primitives + semantic key normalization
    ├── arq_pool.py                  # Singleton ArqRedis connection pool
    ├── task_queue.py                # SimpleTaskQueue (legacy, akan dihapus)
    ├── structured_log.py            # JSON logging
    └── prompt_loader.py             # Versioned prompt loading
```

## Alur Query

```
POST /ask/stream
 ↓
normalize query
 ↓
geo fast path? → ES geo search → narasi → SSE
 ↓
cache hit? → return langsung
 ↓
OOT guard
 ↓
Tier 1 (regex) → commit jika conf ≥ 0.70
 ↓
Tier 2 (scoring) → commit jika conf ≥ 0.60
 ↓
Tier 3 (LLM DeepSeek) → selalu commit
 ↓
route: comparison / aggregation / profile / list / RAG fallback
 ↓
narrator → SSE stream response
```

## Health Check

```bash
curl http://localhost:3000/health
```

Response:
```json
{
  "status": "UP",
  "components": {
    "es": {"status": "UP"},
    "redis": {"status": "UP"},
    "embedding_cache": {"status": "UP", "l1_size": 42, "l2_hits": 120},
    "task_queue": {"status": "UP", "pending": 0, "workers": 2},
    "deepseek": {"status": "UP", "circuit": "closed"}
  }
}
```

HTTP 503 jika ES atau Redis tidak tersedia.

## Konvensi Kode

- Semua I/O wajib `async/await`
- Selalu `try/except` dengan fallback untuk ES, Redis, DeepSeek
- Log dengan tag `[MODULE]` di awal: `logger.info("[LIST] ...")`
- Gunakan `get_logger(__name__)` dari `app.utils.structured_log`
- Return shape handler: `{status, provider, data, confidence, metadata}`
- Jangan gunakan `print()` — selalu `logger`
