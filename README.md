# Prompting — RAG System (Data Pendidikan)
 
> **RAG (Retrieval-Augmented Generation)** system untuk pencarian dan narasi data pendidikan berbasis Elasticsearch + DeepSeek LLM.
 
---
 
## Arsitektur Sistem
 
```
Frontend (Vite/React :5173)
  └──▶ rag-middleware  (Express.js   :4000)
         └──▶ rag-backend  (FastAPI      :3000)
                └──▶ Elasticsearch (:9200)
                └──▶ Redis         (cache)
                └──▶ DeepSeek API  (LLM narasi)
```
 
| Komponen | Teknologi | Port Default |
|---|---|---|
| `rag-middleware` | Node.js ≥ 20, Express.js 5 | 4000 |
| `rag-backend` | Python 3.11, FastAPI, Uvicorn | 3000 |
| Elasticsearch | v8.x | 9200 |
| Redis | v7+ | - |
 
---
 
## Struktur Repository
 
```
Prompting/
├── rag-backend/                    # FastAPI RAG engine
│   ├── app/
│   │   ├── main.py                 # Entry point FastAPI + semua route
│   │   ├── config.py               # Load environment variables
│   │   ├── router.py               # LangGraph-based LLM router
│   │   ├── providers/
│   │   │   └── deepseek.py         # DeepSeek API client
│   │   ├── services/
│   │   │   ├── core/               # Intent classifier, cognitive router, query parser
│   │   │   ├── query_handlers/     # Handler per jenis query (list, profile, aggregation, comparison)
│   │   │   ├── formatters/         # Response & profile formatters
│   │   │   ├── narrator_service.py # Narasi berbasis LLM
│   │   │   ├── embedding_service.py# Sentence Transformer (all-MiniLM-L6-v2)
│   │   │   ├── entity_service.py   # Entity resolution & referensi lokasi
│   │   │   └── location_service.py # Geo & location index
│   │   └── background/             # Background jobs (analytics, rewrite worker, dsb.)
│   ├── scripts/
│   │   ├── ingest_qc.py            # Ingest data ke Elasticsearch
│   │   ├── init_es.py              # Inisialisasi index Elasticsearch
│   │   └── utils/                  # monitor.py, test_search.py, detect_bom_fields.py
│   ├── tests/                      # Unit & integration tests (pytest)
│   ├── data/
│   │   └── mapping.json            # Elasticsearch index mapping
│   ├── models/
│   │   └── all-MiniLM-L6-v2/      # Config model (file .safetensors tidak di-commit)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   ├── .env.example
│   └── README.md
│
├── rag-middleware/                  # Express.js bridge (frontend ke backend)
│   ├── src/
│   │   └── server.js               # Entry point Express
│   ├── Dockerfile
│   ├── package.json
│   ├── .env.example
│   └── README.md
│
└── .gitlab-ci.yml                  # CI/CD pipeline GitLab
```
 
---
 
## Prasyarat
 
Pastikan sudah terinstall sebelum menjalankan project:
 
- **Python 3.11** — [python.org/downloads](https://www.python.org/downloads/)
- **Node.js >= 20 LTS** — [nodejs.org](https://nodejs.org/)
- **Git** — [git-scm.com](https://git-scm.com/)
- Akses ke **Elasticsearch** cluster (URL, username, password)
- Akses ke **Redis** instance
- **DeepSeek API Key** — [platform.deepseek.com](https://platform.deepseek.com/)
- (Opsional) **HuggingFace token** untuk download model otomatis
---
 
## Setup Lokal
 
### 1. Clone repository
 
```bash
git clone https://github.com/fawzulmumin/prompting.git
cd prompting
```
 
### 2. Setup rag-backend (FastAPI)
 
```bash
cd rag-backend
 
# Buat virtual environment
python -m venv .venv
 
# Aktifkan virtual environment
# Linux / Mac:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
 
# Install dependencies
pip install -r requirements.txt
 
# Salin template environment variables
# Linux/Mac:
cp .env.example .env
# Windows:
copy .env.example .env
 
# Edit .env dan isi semua nilai yang kosong
# (lihat bagian Environment Variables di bawah)
 
# Jalankan development server
uvicorn app.main:app --reload --port 3000
```
 
Backend tersedia di: `http://localhost:3000`
 
### 3. Setup rag-middleware (Express.js)
 
Buka terminal baru:
 
```bash
cd rag-middleware
 
# Install dependencies
npm install
 
# Salin template environment variables
# Linux/Mac:
cp .env.example .env
# Windows:
copy .env.example .env
 
# Edit .env
 
# Jalankan development mode (auto-restart)
npm run dev
```
 
Middleware tersedia di: `http://localhost:4000`
 
---
 
## Environment Variables
 
> **Penting:** Jangan pernah commit file `.env`, `.env.production`, atau `.env.local` ke repository.
> Selalu gunakan `.env.example` sebagai template.
 
### rag-backend — `.env`
 
| Variabel | Contoh Nilai | Keterangan |
|---|---|---|
| `PORT` | `3000` | Port FastAPI server |
| `PROD` | _(kosong untuk dev)_ | Set `production` untuk load `.env.production` |
| `ELASTICSEARCH_NODE` | `http://localhost:9200` | URL cluster Elasticsearch |
| `ELASTICSEARCH_USERNAME` | `elastic` | Username Elasticsearch |
| `ELASTICSEARCH_PASSWORD` | `changeme` | Password Elasticsearch |
| `ELASTICSEARCH_INDEX` | `idx_sekolah_qc` | Nama index utama |
| `ELASTICSEARCH_ANALYTICS_INDEX` | `logs-analytics` | Index untuk analytics log |
| `REDIS_URL` | `redis://localhost:6379` | URL Redis |
| `DEEPSEEK_API_KEY` | `sk-...` | API key DeepSeek LLM |
| `HF_TOKEN` | `hf_...` | HuggingFace token (opsional) |
| `TRANSFORMERS_CACHE` | `/app/.cache` | Path cache model transformer |
| `NARRATOR_ENABLED` | `true` | Aktifkan narasi LLM |
| `EMBEDDING_ENABLED` | `true` | Aktifkan semantic search |
| `ALLOWED_ORIGINS` | `http://localhost:5173` | CORS origins (comma-separated) |
| `REPORT_API_KEY` | `your-secret-key` | API key untuk endpoint report |
 
### rag-middleware — `.env`
 
| Variabel | Contoh Nilai | Keterangan |
|---|---|---|
| `PORT` | `4000` | Port Express server |
| `PYTHON_API_URL` | `http://localhost:3000` | URL rag-backend |
| `FRONTEND_URL` | `http://localhost:5173` | URL frontend (untuk CORS) |
| `RATE_LIMIT_REQUESTS` | `30` | Maks request per window |
| `RATE_LIMIT_WINDOW` | `60` | Window rate limit (detik) |
 
---
 
## API Endpoints
 
### rag-middleware (port 4000) — diakses oleh frontend
 
| Method | Path | Body | Deskripsi |
|---|---|---|---|
| `POST` | `/api/ask` | `{ question, user_id, level, lat, lon, radius_km }` | Kirim pertanyaan ke RAG |
| `GET` | `/api/health` | — | Cek status middleware dan backend |
 
### rag-backend (port 3000) — diakses oleh middleware
 
| Method | Path | Deskripsi |
|---|---|---|
| `POST` | `/query` | Proses query RAG lengkap |
| `GET` | `/health` | Health check Elasticsearch dan Redis |
 
---
 
## Model Embedding
 
Service menggunakan model **`all-MiniLM-L6-v2`** (Sentence Transformers).
 
File besar (`model.safetensors`, `pytorch_model.bin`) **tidak di-commit** ke repository.
Yang di-commit hanya file konfigurasi (`config.json`, `tokenizer.json`, `vocab.txt`, dsb.).
 
Model diunduh otomatis saat server pertama kali dijalankan jika koneksi internet tersedia.
Atau bisa diunduh manual:
 
```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```
 
---
 
## Scripts Utilitas
 
Dijalankan dari dalam folder `rag-backend/` dengan virtual environment aktif:
 
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
 
---
 
## Menjalankan Tests
 
```bash
cd rag-backend
 
# Jalankan semua test
pytest tests/ -v
 
# Jalankan test tertentu
pytest tests/test_cognitive_router_qc.py -v
pytest tests/test_regression.py -v
pytest tests/test_multi_module.py -v
```
 
---
 
## Menjalankan via Docker (Production)
 
```bash
# rag-backend
cd rag-backend
docker-compose up -d
 
# rag-middleware
cd rag-middleware
docker build -t rag-middleware .
docker run -d -p 4000:4000 --env-file .env rag-middleware
```
 
`docker-compose.yml` mengatur memory limit 4GB untuk rag-backend (sesuai kebutuhan model embedding).
 
---
 
## CI/CD
 
Pipeline GitLab CI didefinisikan di `.gitlab-ci.yml` pada root repository.
Pipeline berjalan otomatis saat ada push ke branch yang dikonfigurasi dan mencakup build serta deploy untuk kedua service.
 
---
 
## Kontribusi
 
1. Buat branch dari `main`:
   ```bash
   git checkout -b feat/nama-fitur
   ```
2. Kerjakan perubahan dan pastikan tests lulus: `pytest tests/ -v`
3. Push branch dan buat **Merge Request** ke `main` di GitLab
4. Minta review sebelum merge
---
 
## Lisensi
 
Internal — Kementerian Pendidikan Dasar dan Menengah (Kemendikdasmen)