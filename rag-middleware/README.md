# rag-middleware

Express.js middleware untuk sistem RAG Kemendikdasmen. Berfungsi sebagai proxy layer antara client dan rag-backend — menangani CORS, rate limiting, validasi input, dan distributed tracing.

## Persyaratan

- Node.js ≥ 20 LTS
- npm ≥ 10
- rag-backend berjalan di port 3000

## Setup

```bash
cd rag-middleware

npm install
```

## Konfigurasi

Buat file `.env` di folder `rag-middleware/`:

```bash
cp .env.example .env
```

| Variable | Default | Keterangan |
|----------|---------|------------|
| `PORT` | `4000` | Port middleware |
| `BACKEND_URL` | `http://localhost:3000` | URL rag-backend |
| `RATE_LIMIT_WINDOW_MS` | `60000` | Window rate limit (ms) |
| `RATE_LIMIT_MAX` | `30` | Max request per window per IP |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | CORS whitelist, pisahkan dengan koma |
| `NODE_ENV` | `development` | `production` untuk disable verbose log |

## Menjalankan Server

```bash
# Development (auto-reload dengan nodemon)
npm run dev

# Production
npm start
```

## Endpoint

Semua request diteruskan ke rag-backend setelah melewati validasi.

### `POST /api/ask`

Terima pertanyaan dari client dan teruskan ke `POST /ask/stream` di rag-backend sebagai SSE.

**Request:**
```json
{
  "question": "Berapa jumlah SD negeri di Bandung?",
  "user_id": "optional-user-id",
  "lat": null,
  "lon": null
}
```

**Validasi yang dilakukan middleware:**
- `question` wajib ada, maksimal 1000 karakter
- `lat` dan `lon` harus dalam range valid jika disertakan
- Rate limit: 30 request per menit per IP

**Response:** SSE stream dengan events:
```
event: status   data: {"step": "classifying", "message": "..."}
event: status   data: {"step": "searching", "message": "..."}
event: result   data: {"answer": "...", "answer_html": "...", ...}
event: done     data: {}
```

### `GET /health`

Health check middleware.

```bash
curl http://localhost:4000/health
```

## Struktur

```
rag-middleware/
├── src/
│   └── server.js        # Entry point — Express app, routes, middleware
├── package.json
└── .env.example
```

## Fitur

**Rate limiting:** 30 request per menit per IP menggunakan `express-rate-limit`. Client yang melebihi limit mendapat HTTP 429.

**Request tracing:** Setiap request mendapat `x-request-id` (UUID v4) yang diteruskan ke rag-backend untuk distributed tracing di semua log.

**Validasi input:** Pertanyaan kosong, terlalu panjang (>1000 karakter), atau koordinat tidak valid ditolak sebelum sampai ke backend.

**Timeout:** Proxy ke rag-backend memiliki timeout 60 detik. Jika backend tidak merespons dalam 60 detik, client mendapat HTTP 504.

**CORS:** Dikonfigurasi via `ALLOWED_ORIGINS`. Di production, isi dengan domain frontend yang diizinkan.
