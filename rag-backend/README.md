> ✅ Last fix session: QC pipeline — 155 passed, 0 failed (2026-04-26)

# rag-backend
 
FastAPI service sebagai RAG (Retrieval-Augmented Generation) engine yang melayani query berbasis data Elasticsearch.
 
## Alur Data
 
```
rag-middleware (Express :4000) → rag-backend (FastAPI :3000) → Elasticsearch
                                                              → Redis (cache)
                                                              → DeepSeek API (narasi LLM)
```
 
## Tanggung Jawab
 
- Menerima query dari middleware dan mengklasifikasikan intent
- Melakukan pencarian ke Elasticsearch (full-text, semantic, geo)
- Memanggil LLM (DeepSeek) untuk narasi dan routing
- Mengembalikan respons terstruktur ke middleware
## Struktur Folder
 
```
rag-backend/
├── app/
│   ├── main.py                          # Entry point FastAPI + semua route
│   ├── config.py                        # Load environment variables
│   ├── router.py                        # LangGraph-based LLM router
│   ├── providers/
│   │   └── deepseek.py                  # DeepSeek API client
│   ├── services/
│   │   ├── core/                        # Intent classifier, cognitive router, query parser
│   │   │   ├── cognitive_router.py
│   │   │   ├── intent_classifier.py
│   │   │   ├── es_query_builder.py
│   │   │   ├── multi_intent_orchestrator.py
│   │   │   ├── smart_query_parser.py
│   │   │   └── ...
│   │   ├── query_handlers/              # Handler per jenis query
│   │   │   ├── list_school_handler.py
│   │   │   ├── aggregation_handler.py
│   │   │   ├── profile_handler.py
│   │   │   ├── comparison_handler.py
│   │   │   └── school_vs_handler.py
│   │   ├── search/                      # Search services
│   │   │   ├── search_service.py        # Elasticsearch search
│   │   │   └── profile_search_service.py
│   │   ├── formatters/                  # Response formatters
│   │   │   ├── response_formatter.py
│   │   │   └── profile_formatter.py
│   │   ├── narrator_service.py          # Narasi berbasis LLM
│   │   ├── embedding_service.py         # Sentence Transformer embeddings
│   │   ├── entity_service.py            # Entity resolution & referensi lokasi
│   │   ├── location_service.py          # Geo & location index
│   │   ├── confidence_engine.py         # Confidence scoring
│   │   └── rate_limit_service.py        # Rate limiting
│   ├── background/                      # Background jobs (analytics, rewrite worker, dsb.)
│   └── utils/
│       ├── cache.py                     # Redis cache
│       ├── cost_tracker.py              # Token & cost logging
│       ├── rag_evaluation_logger.py
│       ├── markdown_to_html.py
│       ├── location_utils.py
│       └── domain_filter.py
├── scripts/
│   ├── ingest_qc.py                     # Ingest data ke Elasticsearch
│   ├── init_es.py                       # Inisialisasi index Elasticsearch
│   └── utils/
│       ├── monitor.py                   # Monitor status ingestion
│       ├── test_search.py               # Test koneksi & pencarian ES
│       └── detect_bom_fields.py         # Deteksi BOM fields pada CSV
├── tests/                               # Unit & integration tests (pytest)
├── data/
│   └── mapping.json                     # Elasticsearch index mapping
├── models/
│   └── all-MiniLM-L6-v2/               # Config model (file .safetensors tidak di-commit)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```
 
## Setup Lokal
 
```bash
# 1. Buat virtual environment
python -m venv .venv
 
# 2. Aktifkan virtual environment
# Linux/Mac:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
 
# 3. Install dependencies
pip install -r requirements.txt
 
# 4. Salin template env
# Linux/Mac:
cp .env.example .env
# Windows:
copy .env.example .env
 
# Edit .env sesuai konfigurasi lokal
 
# 5. Jalankan server (development)
uvicorn app.main:app --reload --port 3000
```
 
## Environment Variables
 
Lihat `.env.example` untuk daftar lengkap variabel yang dibutuhkan.
 
Variabel utama:
 
| Variabel | Deskripsi |
|---|---|
| `PORT` | Port server (default: 3000) |
| `ELASTICSEARCH_NODE` | URL Elasticsearch cluster |
| `ELASTICSEARCH_USERNAME` | Username Elasticsearch |
| `ELASTICSEARCH_PASSWORD` | Password Elasticsearch |
| `ELASTICSEARCH_INDEX` | Nama index utama |
| `ELASTICSEARCH_ANALYTICS_INDEX` | Index untuk analytics log |
| `REDIS_URL` | URL Redis untuk caching |
| `DEEPSEEK_API_KEY` | API key DeepSeek LLM |
| `HF_TOKEN` | HuggingFace token (opsional, untuk download model) |
| `TRANSFORMERS_CACHE` | Path cache model transformer |
| `NARRATOR_ENABLED` | Aktifkan narasi LLM (`true`/`false`) |
| `EMBEDDING_ENABLED` | Aktifkan semantic search (`true`/`false`) |
| `ALLOWED_ORIGINS` | CORS origins yang diizinkan (comma-separated) |
| `PROD` | Set ke `production` untuk load `.env.production` |
 
## Model Embedding
 
Service menggunakan model **`all-MiniLM-L6-v2`** (Sentence Transformers).
File besar (`model.safetensors`) tidak di-commit. Model diunduh otomatis saat pertama kali server dijalankan.
 
Download manual jika diperlukan:
```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```
 
## Docker
 
```bash
# Build image
docker build -t rag-backend .
 
# Jalankan via docker-compose (production)
docker-compose up -d
```
 
## Scripts Utilitas
 
Dijalankan dari root folder `rag-backend/` dengan virtual environment aktif:
 
```bash
# Inisialisasi index Elasticsearch (jalankan sekali di awal setup)
python scripts/init_es.py
 
# Ingest data ke Elasticsearch
python scripts/ingest_qc.py
 
# Monitor status ingestion
python scripts/utils/monitor.py
 
# Test koneksi dan pencarian Elasticsearch
python scripts/utils/test_search.py
 
# Deteksi BOM fields pada data CSV
python scripts/utils/detect_bom_fields.py
```
 
## Menjalankan Tests
 
```bash
pytest tests/ -v
```
 