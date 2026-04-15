# rag-backend
 
FastAPI service sebagai RAG (Retrieval-Augmented Generation) engine yang melayani query berbasis data Elasticsearch.
 
## Alur Data
 
```
rag-middleware (Express :4000) → rag-backend (FastAPI :3000) → Elasticsearch
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
│   ├── main.py                  # Entry point FastAPI
│   ├── config.py                # Load environment variables
│   ├── router.py                # LangGraph-based LLM router
│   ├── providers/
│   │   └── deepseek.py          # DeepSeek API client
│   ├── services/
│   │   ├── core/                # Intent classifier, cognitive router, query parser
│   │   ├── query_handlers/      # Handler per jenis query (list, profile, aggregation, dsb.)
│   │   ├── search/              # Profile search service
│   │   ├── formatters/          # Response formatters
│   │   ├── narrator_service.py  # LLM narration
│   │   ├── search_service.py    # Elasticsearch search
│   │   ├── location_service.py  # Geo & location index
│   │   ├── embedding_service.py # Sentence transformer embeddings
│   │   ├── entity_service.py    # Entity resolution
│   │   ├── response_formatter.py
│   │   ├── confidence_engine.py
│   │   └── rate_limit_service.py
│   ├── background/              # Background jobs (analytics, rewrite worker, dsb.)
│   └── utils/
│       ├── cache.py             # Redis cache
│       ├── cost_tracker.py      # Token & cost logging
│       ├── rag_evaluation_logger.py
│       ├── markdown_to_html.py
│       ├── location_utils.py
│       └── domain_filter.py
├── scripts/                     # Utilitas CLI (ingest data, init ES, monitor)
├── tests/                       # Unit & integration tests
├── data/
│   └── mapping.json             # Elasticsearch index mapping
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
source .venv/bin/activate        # Linux/Mac
# .venv\Scripts\activate         # Windows
 
# 2. Install dependencies
pip install -r requirements.txt
 
# 3. Salin template env
cp .env.example .env
# Edit .env sesuai konfigurasi lokal
 
# 4. Jalankan server (development)
uvicorn app.main:app --reload --port 3000
```
 
## Environment Variables
 
Lihat `.env.example` untuk daftar lengkap variabel yang dibutuhkan.
 
Variabel utama:
 
| Variabel | Deskripsi |
|----------|-----------|
| `ELASTICSEARCH_NODE` | URL Elasticsearch cluster |
| `ELASTICSEARCH_INDEX` | Nama index utama |
| `REDIS_URL` | URL Redis untuk caching |
| `DEEPSEEK_API_KEY` | API key DeepSeek LLM |
| `HF_TOKEN` | HuggingFace token untuk model embedding |
| `PROD` | Set ke `production` untuk load `.env.production` |
 
## Docker
 
```bash
# Build image
docker build -t rag-backend .
 
# Jalankan via docker-compose (production)
docker-compose up -d
```
 
## Scripts Utilitas
 
Dijalankan dari root folder `rag-backend/`:
 
```bash
# Ingest data ke Elasticsearch
python scripts/ingest.py
 
# Ingest cepat (parallel)
python scripts/ingest_fast.py
 
# Ingest data yang belum masuk
python scripts/ingest_missing.py
 
# Inisialisasi index Elasticsearch
python scripts/init_es.py
 
# Monitor status ingestion
python scripts/monitor.py
```
 
## Menjalankan Tests
 
```bash
pytest tests/ -v
```