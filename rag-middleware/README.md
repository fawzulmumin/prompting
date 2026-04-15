# rag-middleware
 
Express.js bridge service yang menjadi perantara antara frontend dan `rag-backend` (FastAPI).
 
## Alur Data
 
```
Frontend → rag-middleware (Express :4000) → rag-backend (FastAPI :3000) → Elasticsearch
```
 
## Tanggung Jawab
 
- Validasi input dari frontend sebelum diteruskan ke backend
- CORS handling untuk request dari frontend
- Rate limiting per endpoint
- Security headers via Helmet
- Request tracing via `X-Request-Id`
- Graceful shutdown
 
## Endpoints
 
| Method | Path | Deskripsi |
|--------|------|-----------|
| POST | `/api/ask` | Kirim pertanyaan ke RAG backend |
| GET | `/api/health` | Cek status backend |
 
## Setup Lokal
 
```bash
# Install dependencies
npm install
 
# Salin template env
cp .env.example .env
# Edit .env sesuai konfigurasi lokal
 
# Jalankan development mode (auto-restart)
npm run dev
 
# Jalankan production mode
npm start
```
 
## Environment Variables
 
Lihat `.env.example` untuk daftar variabel yang dibutuhkan.
 
## Docker
 
```bash
# Build image
docker build -t rag-middleware .
 
# Jalankan container
docker run -p 4000:4000 --env-file .env.production rag-middleware
```