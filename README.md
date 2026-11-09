# RAG Kemendikdasmen — Sistem Pencarian Data Pendidikan

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Express](https://img.shields.io/badge/Express.js-000000?logo=express&logoColor=white)
![Elasticsearch](https://img.shields.io/badge/Elasticsearch-8.x-005571?logo=elasticsearch&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7.x-DC382D?logo=redis&logoColor=white)
![Kubernetes](https://img.shields.io/badge/Kubernetes-326CE5?logo=kubernetes&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-blue)

Sistem Retrieval-Augmented Generation (RAG) untuk pencarian dan narasi data ~560.000 sekolah seluruh Indonesia. Dibangun untuk Kementerian Pendidikan Dasar dan Menengah (Kemendikdasmen).

## Deskripsi

Sistem ini memungkinkan pejabat Kemendikdasmen, dinas pendidikan daerah, dan masyarakat umum untuk mengajukan pertanyaan dalam bahasa natural dan mendapatkan jawaban yang akurat tentang data sekolah — mulai dari daftar sekolah per wilayah, jumlah sekolah per jenjang, profil sekolah spesifik, hingga perbandingan antar wilayah.

**Contoh pertanyaan yang bisa dijawab:**
- "Berapa jumlah SD negeri di Kabupaten Bandung?"
- "Tampilkan daftar SMA akreditasi A di Surabaya"
- "Profil SMAN 3 Bandung"
- "Bandingkan jumlah SMK di Jakarta dan Surabaya"
- "Sekolah terdekat dari lokasi saya"

## Fitur utama

- **Pencarian bahasa natural** untuk daftar sekolah, agregasi, profil sekolah, dan perbandingan antar wilayah.
- **Klasifikasi query berlapis (Tier 1–3)** yang menekan pemakaian LLM: 74% query selesai tanpa LLM.
- **Streaming jawaban** lewat Server-Sent Events (SSE).
- **Cache berlapis** dan antrean tugas latar belakang dengan Redis.
- **Rate limiting dua lapis** di middleware (per IP) dan backend (per user).
- **Siap deploy ke Kubernetes**, dengan worker arq sebagai sidecar.

## Arsitektur

```mermaid
flowchart TD
    U[Pengguna] --> M["rag-middleware<br/>Express.js :4000<br/>CORS, rate limit, validasi, UUID tracing"]
    M --> B["rag-backend<br/>FastAPI :3000<br/>klasifikasi intent, query handler, SSE"]
    B --> E[("Elasticsearch<br/>~560K dokumen")]
    B --> R[("Redis<br/>cache + queue")]
    B -->|query kompleks| L["DeepSeek API<br/>narasi / router Tier 3"]
```

### Klasifikasi berlapis — 74% query tanpa LLM

```mermaid
flowchart LR
    Q[Query] --> T1["Tier 1<br/>regex pattern<br/>di bawah 5 ms"]
    T1 -->|tidak cocok| T2["Tier 2<br/>rule scoring<br/>di bawah 20 ms"]
    T2 -->|ambigu| T3["Tier 3<br/>DeepSeek LLM<br/>5–65 s"]
    T1 -->|cocok| H[Query handler]
    T2 -->|cocok| H
    T3 --> H
```

- Tier 1: regex pattern, <5ms
- Tier 2: rule scoring, <20ms
- Tier 3: DeepSeek LLM, 5–65s (hanya untuk query kompleks)

## Struktur Repository

```
prompting/
├── rag-backend/        # FastAPI — business logic, klasifikasi, handler
├── rag-middleware/     # Express.js — proxy, rate limit, validasi
├── k8s/                # Kubernetes manifests (ConfigMap, Secret, Deployment)
└── README.md
```

## Status

| Komponen | Status |
|----------|--------|
| Phase 1–15 | ✅ Selesai |
| Phase 16 (tech debt) | ✅ Selesai |
| Total tests | 642 passed |
| Data sekolah | ~560K (38 provinsi, 600+ kab/kota) |
| LLM calls ditekan | 74% query no-LLM |

## Konfigurasi Environment

File `.env` tidak disimpan di repo. Salin dari template lalu isi nilainya:

```bash
cp rag-backend/.env.example rag-backend/.env
cp rag-middleware/.env.example rag-middleware/.env
```

## Quick Start

Lihat README masing-masing komponen untuk instruksi lengkap:

- [`rag-backend/README.md`](rag-backend/README.md) — setup Python, environment, menjalankan server
- [`rag-middleware/README.md`](rag-middleware/README.md) — setup Node.js, konfigurasi proxy

## Deployment

Manifest Kubernetes tersedia di folder `k8s/`:

```bash
# Apply ConfigMap dan Deployment
kubectl apply -f k8s/configmap-backend.yaml
kubectl apply -f k8s/configmap-middleware.yaml
kubectl apply -f k8s/deployment-backend.yaml

# Secret — isi nilai asli terlebih dahulu, JANGAN commit setelah diisi
# cp k8s/secret-backend.yaml k8s/secret-backend.local.yaml
# kubectl apply -f k8s/secret-backend.local.yaml
```

Deployment backend menggunakan dua container dalam satu pod:
- **FastAPI server** — melayani request HTTP
- **arq worker** (sidecar) — menjalankan background jobs dengan guarantee delivery via Redis

## Tech Stack

| Komponen | Teknologi |
|----------|-----------|
| Backend | Python 3.11, FastAPI 0.128.0 |
| Middleware | Node.js ≥20 LTS, Express.js 5 |
| Database | Elasticsearch 8.x |
| Cache & Queue | Redis 7.x, arq 0.28.0 |
| Embedding | sentence-transformers all-MiniLM-L6-v2 |
| LLM | DeepSeek API (deepseek-chat) |
| Orchestration | Kubernetes |

## Lisensi

Dirilis di bawah lisensi MIT. Lihat berkas [LICENSE](LICENSE).
