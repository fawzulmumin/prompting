# Kubernetes Configuration — RAG Kemendikdasmen

File-file di direktori ini adalah **template konfigurasi** untuk deployment ke Kubernetes.
ConfigMap aman di-commit. Secret **tidak boleh di-commit** setelah diisi nilai asli (sudah di-gitignore).

## File-file di direktori ini

| File | Tipe | Perlu diisi? |
|------|------|--------------|
| `configmap-backend.yaml` | ConfigMap (rag-backend) | `ALLOWED_ORIGINS` |
| `configmap-middleware.yaml` | ConfigMap (rag-middleware) | `FRONTEND_URL`, `PYTHON_API_URL` |
| `secret-backend.yaml` | Secret template | Semua field (`GANTI_...`) |

## Langkah sebelum apply

### 1. Isi nilai yang masih placeholder

**`configmap-backend.yaml`** — edit 1 field:
```
ALLOWED_ORIGINS: "https://app.example.com"
```

**`configmap-middleware.yaml`** — edit 2 field:
```
PYTHON_API_URL: "http://rag-backend-service:3000"   # nama Service k8s backend
FRONTEND_URL:   "https://app.example.com"
```

**`secret-backend.yaml`** — isi semua 7 field dengan nilai asli:
```
ELASTICSEARCH_NODE:     "http://<ip-atau-hostname>:9200"
ELASTICSEARCH_USERNAME: "elastic"
ELASTICSEARCH_PASSWORD: "<password-elasticsearch>"
REDIS_URL:              "redis://<ip-atau-hostname>:<port>"
DEEPSEEK_API_KEY:       "sk-..."
HF_TOKEN:               "hf_..."
REPORT_API_KEY:         "<random-string-minimal-32-karakter>"
```

> Untuk generate `REPORT_API_KEY` yang kuat:
> ```bash
> python -c "import secrets; print(secrets.token_hex(32))"
> ```

### 2. Pastikan namespace sesuai

Default namespace di semua file adalah `default`. Jika deployment menggunakan namespace lain,
ganti di semua file sebelum apply:
```bash
# Contoh: ganti "default" ke "kemendikdasmen"
sed -i 's/namespace: default/namespace: kemendikdasmen/g' k8s/*.yaml
```

## Urutan apply

Secret harus ada sebelum Pod bisa start. Selalu apply dalam urutan ini:

```bash
# 1. Secret terlebih dahulu
kubectl apply -f k8s/secret-backend.yaml

# 2. ConfigMap
kubectl apply -f k8s/configmap-backend.yaml
kubectl apply -f k8s/configmap-middleware.yaml

# 3. Deployment & Service (file terpisah, tidak termasuk di sini)
kubectl apply -f k8s/deployment-backend.yaml
kubectl apply -f k8s/deployment-middleware.yaml
```

Atau apply semua sekaligus (kubectl respects dependency order melalui urutan file):
```bash
kubectl apply -f k8s/
```

## Cara update ALLOWED_ORIGINS jika URL frontend berubah

Dua tempat yang harus diupdate secara bersamaan:

```bash
# 1. Edit configmap-backend.yaml dan configmap-middleware.yaml
#    Ganti nilai ALLOWED_ORIGINS dan FRONTEND_URL

# 2. Apply ulang ConfigMap
kubectl apply -f k8s/configmap-backend.yaml
kubectl apply -f k8s/configmap-middleware.yaml

# 3. Restart Pod agar membaca nilai baru (ConfigMap tidak auto-reload)
kubectl rollout restart deployment/rag-backend
kubectl rollout restart deployment/rag-middleware
```

## Verifikasi setelah apply

```bash
# Cek ConfigMap tersimpan
kubectl get configmap rag-backend-config -o yaml
kubectl get configmap rag-middleware-config -o yaml

# Cek Secret tersimpan (nilai akan ter-encode base64, ini normal)
kubectl get secret rag-backend-secret -o yaml

# Cek Pod berjalan
kubectl get pods

# Cek health endpoint
kubectl port-forward service/rag-middleware-service 4000:4000
curl http://localhost:4000/api/health
```

## Catatan arsitektur rate limit

Sistem menggunakan **dua layer rate limit** dengan dimensi berbeda — keduanya aktif bersamaan:

| Layer | Lokasi | Dimensi | Limit |
|-------|--------|---------|-------|
| Layer 1 | rag-middleware (Express) | Per IP | 30 req/menit |
| Layer 2 | rag-backend (Redis INCR) | Per user_id | 5 req/60 detik |

Jika ingin mengubah limit Layer 2, update `RATE_LIMIT_REQUESTS` di `configmap-backend.yaml`
dan sesuaikan juga nilai `max` di `server.js` agar konsisten.
