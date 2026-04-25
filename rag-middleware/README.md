# rag-middleware
 
Express.js bridge service yang menjadi perantara antara frontend dan `rag-backend` (FastAPI).
 
## Alur Data
 
```
Frontend (:5173) → rag-middleware (Express :4000) → rag-backend (FastAPI :3000) → Elasticsearch
```
 
## Tanggung Jawab
 
- Validasi input dari frontend sebelum diteruskan ke backend
- CORS handling untuk request dari frontend
- Rate limiting per endpoint (default: 30 request/menit pada `/api/ask`)
- Security headers via Helmet
- Request tracing via `X-Request-Id`
- Graceful shutdown
## Struktur Folder
 
```
rag-middleware/
├── src/
│   └── server.js     # Entry point Express (semua route & middleware)
├── Dockerfile
├── package.json
├── .env.example
└── README.md
```
 
## Endpoints
 
| Method | Path | Body | Deskripsi |
|---|---|---|---|
| `POST` | `/api/ask` | `{ question, user_id, level, lat, lon, radius_km }` | Kirim pertanyaan ke RAG backend |
| `GET` | `/api/health` | — | Cek status middleware dan backend |
 
## Setup Lokal
 
```bash
# 1. Install dependencies
npm install
 
# 2. Salin template env
# Linux/Mac:
cp .env.example .env
# Windows:
copy .env.example .env
 
# Edit .env sesuai konfigurasi lokal
 
# 3. Jalankan development mode (auto-restart)
npm run dev
 
# Jalankan production mode
npm start
```
 
Middleware tersedia di: `http://localhost:4000`
 
## Environment Variables
 
Lihat `.env.example` untuk daftar variabel yang dibutuhkan.
 
| Variabel | Contoh Nilai | Keterangan |
|---|---|---|
| `PORT` | `4000` | Port Express server |
| `PYTHON_API_URL` | `http://localhost:3000` | URL rag-backend |
| `FRONTEND_URL` | `http://localhost:5173` | URL frontend (untuk CORS) |
| `RATE_LIMIT_REQUESTS` | `30` | Maks request per window |
| `RATE_LIMIT_WINDOW` | `60` | Window rate limit (detik) |
 
## Docker
 
```bash
# Build image
docker build -t rag-middleware .
 
# Jalankan container
# Linux/Mac:
docker run -d -p 4000:4000 --env-file .env rag-middleware
# Windows (PowerShell):
docker run -d -p 4000:4000 --env-file .env rag-middleware
```