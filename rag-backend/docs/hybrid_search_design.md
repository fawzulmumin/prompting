# Hybrid Search Design Document
## RAG Backend Sekolah Indonesia — Priority 2

**Status:** Design only — implementasi menunggu vektorisasi selesai  
**Author:** Dibuat berdasarkan diskusi arsitektur April 2026  
**Target audience:** Rekan vektorisasi + developer implementasi hybrid search  
**ES Index:** `idx_sekolah_qc` | **Dokumen:** 562,627 | **Vector dims:** 384 (sudah di mapping)

---

## 1. Vectorization Strategy

### 1.1 Model Embedding yang Direkomendasikan

**Pilihan utama: `paraphrase-multilingual-MiniLM-L12-v2`**

| Properti | Value |
|----------|-------|
| Dimensi output | 384 (✅ sesuai mapping ES yang sudah ada) |
| Ukuran model | ~120MB |
| Bahasa | 50+ bahasa termasuk Bahasa Indonesia |
| Kecepatan | ~14k sentences/detik (CPU), ~50k/detik (GPU) |
| Library | `sentence-transformers` |

```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
```

**Alternatif jika kualitas kurang:** `intfloat/multilingual-e5-small` (384 dim, lebih akurat untuk retrieval, sedikit lebih lambat).

**Yang TIDAK direkomendasikan:** OpenAI `text-embedding-ada-002` (1536 dim — tidak kompatibel dengan mapping ES yang sudah ada tanpa perubahan mapping).

### 1.2 Apa yang Di-embed

**Di-embed: field `content` yang sudah ada**, bukan raw source fields.

Alasan:
- Field `content` sudah merupakan ringkasan teks sekolah yang siap pakai
- Embedding dari teks terstruktur (nama + alamat + jenjang) memberikan representasi yang lebih baik dari field individual
- Tidak perlu perubahan pipeline ingestion

Format content yang di-embed (contoh):
```
SDN 1 Pagedangan. Sekolah Dasar Negeri. Akreditasi A. 
Kecamatan Pagedangan, Kabupaten Tangerang, Banten. 
NPSN: 20603001. Status: Aktif.
```

**Yang TIDAK di-embed secara terpisah:** field numerik (siswa_l, siswa_p, tendik), koordinat, kode_pos — field ini sudah di-filter via BM25/term filter, tidak perlu vektor semantik.

### 1.3 Batch Processing untuk 562k Dokumen

```python
# Pseudocode batch vectorization
BATCH_SIZE = 256        # optimal untuk MiniLM di CPU
CHECKPOINT_EVERY = 5000 # resume dari checkpoint jika gagal

def vectorize_all(es_client, model, index="idx_sekolah_qc"):
    query = {"query": {"match_all": {}}, "_source": ["npsn", "content"]}
    
    # Scroll API untuk iterasi 562k dokumen
    for batch in scroll_batches(es_client, index, query, batch_size=BATCH_SIZE):
        contents = [doc["_source"]["content"] for doc in batch]
        npsns    = [doc["_source"]["npsn"] for doc in batch]
        
        # Encode batch
        vectors = model.encode(contents, batch_size=BATCH_SIZE, 
                               show_progress_bar=True, normalize_embeddings=True)
        
        # Bulk update ke ES
        actions = [
            {"_op_type": "update", "_index": index, "_id": doc["_id"],
             "doc": {"content_vector": vec.tolist()}}
            for doc, vec in zip(batch, vectors)
        ]
        bulk(es_client, actions)
        
        # Checkpoint
        save_checkpoint(npsns[-1])
```

**Parameter penting:**
- `normalize_embeddings=True` — wajib untuk cosine similarity yang benar di ES
- Gunakan `scroll` bukan `search_after` untuk bulk vectorization (lebih stabil)
- Simpan checkpoint per batch untuk resume jika proses terputus

### 1.4 Estimasi Waktu Indexing

| Hardware | Estimasi waktu |
|----------|---------------|
| CPU only (modern, 8 core) | ~3–4 jam |
| GPU (T4/V100) | ~20–30 menit |
| CPU dengan batch=512 | ~2–3 jam (memory permitting) |

**Catatan:** ES bulk update bisa jadi bottleneck — gunakan `refresh_interval: -1` selama indexing, lalu set kembali ke `1s` setelah selesai.

```json
// Sebelum vectorization
PUT /idx_sekolah_qc/_settings
{"index": {"refresh_interval": "-1", "number_of_replicas": 0}}

// Setelah selesai
PUT /idx_sekolah_qc/_settings  
{"index": {"refresh_interval": "1s", "number_of_replicas": 1}}
```

---

## 2. Query Routing: Kapan Pakai Vector vs Keyword

### 2.1 Decision Matrix

| Module | Search Mode | Alasan |
|--------|-------------|--------|
| `aggregation` | Pure BM25 + term filter | Butuh count exact, bukan relevance ranking |
| `comparison` | Pure BM25 + term filter | Butuh count per wilayah, bukan dokumen |
| `profile` | Pure BM25 (exact match NPSN/nama) | Satu dokumen spesifik, precision > recall |
| `list` | Pure BM25 + term filter | Filter lokasi/jenjang/status sudah cukup |
| `geo` | Geo distance filter | Bukan text relevance problem |
| RAG fallback | **Hybrid RRF** | Query semantik tanpa filter spesifik |
| Query semantik | **Hybrid RRF** | "sekolah inklusi", "sekolah berbasis agama" |

### 2.2 Kapan Masuk Hybrid Path

Hybrid search diaktifkan jika **semua kondisi berikut terpenuhi:**

```python
def should_use_hybrid(intent: CognitiveIntent) -> bool:
    # Hanya untuk RAG fallback (tidak ada module spesifik yang match)
    if intent.module not in ("out_of_context",) and intent.secondary == []:
        return False
    
    # Query semantik: tidak ada filter wilayah/jenjang spesifik
    has_specific_filter = (
        intent.wilayah.kabupaten or 
        intent.wilayah.provinsi or
        intent.jenjang or 
        intent.nama_sekolah
    )
    
    # Semantic signals: kata-kata yang tidak bisa di-filter dengan term
    semantic_keywords = [
        "inklusi", "inklusif", "berbasis agama", "internasional",
        "unggulan", "terbaik", "reputasi", "berprestasi",
        "ramah anak", "adiwiyata", "green school"
    ]
    has_semantic_signal = any(kw in intent.raw_label.lower() 
                              for kw in semantic_keywords 
                              if intent.raw_label)
    
    return has_semantic_signal or not has_specific_filter
```

### 2.3 Contoh Query per Path

**Pure BM25:**
- "berapa SD di Bandung" → aggregation, term filter provinsi
- "daftar SMK negeri di Surabaya" → list, term filter kota + status
- "profil SMAN 3 Bandung" → profile, exact match nama

**Hybrid RRF:**
- "sekolah inklusi di Jawa Barat" → semantic + geo filter provinsi
- "sekolah berbasis agama di dekat sini" → semantic + geo distance
- "sekolah dengan program unggulan" → pure semantic, tidak ada filter spesifik
- "SD terbaik di Tangerang" → semantic ("terbaik" tidak bisa di-filter term)

---

## 3. Hybrid Search Implementation

### 3.1 RRF di Elasticsearch 8.x

Elasticsearch 8.9+ mendukung RRF natively via `retriever` API. Ini yang direkomendasikan (lebih clean dari manual score fusion):

```json
GET /idx_sekolah_qc/_search
{
  "retriever": {
    "rrf": {
      "retrievers": [
        {
          "standard": {
            "query": {
              "bool": {
                "must": {
                  "multi_match": {
                    "query": "sekolah inklusi ramah anak",
                    "fields": ["nama^2", "content"],
                    "type": "best_fields"
                  }
                },
                "filter": [
                  {"term": {"provinsi": "jawa barat"}}
                ]
              }
            }
          }
        },
        {
          "knn": {
            "field": "content_vector",
            "query_vector": [0.123, -0.456, ...],
            "k": 50,
            "num_candidates": 100,
            "filter": [
              {"term": {"provinsi": "jawa barat"}}
            ]
          }
        }
      ],
      "rank_constant": 60,
      "rank_window_size": 100
    }
  },
  "size": 10
}
```

**Referensi:** [Elasticsearch RRF docs](https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html)

### 3.2 Parameter Tuning

| Parameter | Nilai awal | Keterangan |
|-----------|-----------|-----------|
| `rank_constant` | 60 | Default ES. Turunkan (20-40) untuk lebih aggressive ranking BM25 |
| `rank_window_size` | 100 | Harus ≥ `size`. Naikkan untuk recall lebih baik |
| `k` (kNN) | 50 | Kandidat kNN sebelum RRF. Naikkan jika recall rendah |
| `num_candidates` | 100 | Harus ≥ `k`. ES recommendation: 1.5-2x dari `k` |

### 3.3 Filter Wilayah/Jenjang Sebelum kNN

**Wajib:** filter harus di-apply di **kedua** retriever (BM25 dan kNN), bukan di post-filter:

```json
// ✅ BENAR — filter di dalam retriever
"knn": {
  "filter": [{"term": {"provinsi": "jawa barat"}}]
}

// ❌ SALAH — post_filter tidak apply ke kNN candidates
"post_filter": {"term": {"provinsi": "jawa barat"}}
```

Alasan: filter di dalam kNN mengurangi search space sebelum ANN search → lebih efisien dan hasil lebih relevan.

### 3.4 Score Normalization

Dengan RRF, score normalization otomatis — tidak perlu manual. RRF score formula:

```
RRF_score(doc) = Σ 1 / (rank_constant + rank_i)
```

Tidak perlu normalize BM25 score dan cosine similarity secara manual — ini keunggulan RRF dibanding linear combination.

---

## 4. Schema Changes

### 4.1 Mapping Existing (dari docs/es_structure.md)

Field `content_vector` sudah ada di mapping:
```json
"content_vector": {
  "type": "dense_vector",
  "dims": 384,
  "index": false,
  "similarity": "cosine"
}
```

### 4.2 Update Mapping yang Diperlukan

**Wajib diubah:** `"index": false` → `"index": true` agar kNN search bisa jalan.

```json
PUT /idx_sekolah_qc/_mapping
{
  "properties": {
    "content_vector": {
      "type": "dense_vector",
      "dims": 384,
      "index": true,
      "similarity": "cosine",
      "index_options": {
        "type": "hnsw",
        "m": 16,
        "ef_construction": 100
      }
    }
  }
}
```

**Catatan:** Mengubah `index` dari `false` ke `true` membutuhkan **reindex** — tidak bisa update in-place. Plan reindex:

```
1. Buat index baru idx_sekolah_qc_v2 dengan mapping baru
2. Reindex dari idx_sekolah_qc ke idx_sekolah_qc_v2 + populate vectors
3. Alias swap: idx_sekolah_qc → idx_sekolah_qc_v2
4. Delete index lama setelah validasi
```

### 4.3 HNSW Parameter Tuning

| Parameter | Default | Rekomendasi | Keterangan |
|-----------|---------|-------------|-----------|
| `m` | 16 | 16 | Jumlah koneksi per node. Lebih tinggi = recall lebih baik, memory lebih besar |
| `ef_construction` | 100 | 100 | Build time accuracy. Naikkan ke 200 jika recall kurang |

Untuk 562k dokumen dengan 384 dims: estimasi memory HNSW ~**1.2GB** tambahan di heap ES.

---

## 5. Code Changes Required

### 5.1 File yang Perlu Diubah

#### `app/services/search/search_service.py`
Fungsi utama yang perlu ditambah:

```python
async def perform_hybrid_search(
    es_client: AsyncElasticsearch,
    query_text: str,
    query_vector: List[float],      # dari embedding model
    size: int = 10,
    location_filter: Optional[Dict] = None,
    jenjang_filter: Optional[str] = None,
) -> List[dict]:
    """
    Hybrid search menggunakan RRF (BM25 + kNN).
    Digunakan untuk RAG fallback dan query semantik.
    """
    # Build filters (apply ke kedua retriever)
    filters = _build_hybrid_filters(location_filter, jenjang_filter)
    
    body = {
        "retriever": {
            "rrf": {
                "retrievers": [
                    {"standard": {"query": {
                        "bool": {
                            "must": {"multi_match": {
                                "query": query_text,
                                "fields": ["nama^2", "content"]
                            }},
                            "filter": filters
                        }
                    }}},
                    {"knn": {
                        "field": "content_vector",
                        "query_vector": query_vector,
                        "k": 50,
                        "num_candidates": 100,
                        "filter": filters
                    }}
                ],
                "rank_constant": 60,
                "rank_window_size": 100
            }
        },
        "size": size
    }
    
    response = await es_client.search(index="idx_sekolah_qc", body=body)
    return response["hits"]["hits"]
```

#### `app/services/embedding_service.py`
Tambah fungsi encode query (model sudah di-init di startup):

```python
def encode_query(query_text: str) -> List[float]:
    """Encode query string ke vector. Sinkron — panggil via asyncio.to_thread."""
    model = get_embedding_model()
    vector = model.encode(query_text, normalize_embeddings=True)
    return vector.tolist()
```

#### `app/main.py` — RAG Fallback Path
Di bagian RAG flow (paling bawah endpoint `/ask`), ganti `perform_search` dengan `perform_hybrid_search`:

```python
# Sebelum (pure BM25):
hits = await perform_search(es_client=es_client, query_text=query, ...)

# Sesudah (hybrid):
if should_use_hybrid(intent):
    query_vector = await asyncio.to_thread(encode_query, query)
    hits = await perform_hybrid_search(
        es_client=es_client,
        query_text=query,
        query_vector=query_vector,
        location_filter=_loc_filter or None,
    )
else:
    hits = await perform_search(es_client=es_client, query_text=query, ...)
```

#### `app/services/core/cognitive_router.py`
Tambah deteksi semantic query di `_keyword_fallback()`:

```python
_SEMANTIC_SIGNALS = {
    "inklusi", "inklusif", "berbasis agama", "internasional",
    "unggulan", "terbaik", "berprestasi", "ramah anak",
    "adiwiyata", "green school", "bilingual"
}

# Di _keyword_fallback(), tambah field ke CognitiveIntent:
is_semantic = any(sig in q for sig in _SEMANTIC_SIGNALS)
# Kemudian di return CognitiveIntent(...), pass is_semantic
```

Dan tambah field di `CognitiveIntent` dataclass:
```python
is_semantic: bool = False  # True → trigger hybrid search path
```

### 5.2 File yang TIDAK Perlu Diubah

- `aggregation_handler.py` — tetap pure BM25
- `comparison_handler.py` — tetap pure BM25
- `list_school_handler.py` — tetap pure BM25
- `profile_handler.py` — tetap pure BM25 exact match
- `multi_intent_orchestrator.py` — tidak ada perubahan routing

---

## 6. Testing Strategy

### 6.1 Test Hybrid tanpa Break Existing Path

Prinsip: hybrid search adalah **additive path**, bukan pengganti. Existing keyword tests tidak perlu diubah.

```python
# tests/test_hybrid_search.py (file baru, setelah vektorisasi)

@pytest.mark.integration  # butuh ES + model
async def test_hybrid_search_returns_results():
    """Hybrid search harus return hits untuk query semantik."""
    vector = encode_query("sekolah inklusi ramah anak")
    hits = await perform_hybrid_search(
        es_client=es_client,
        query_text="sekolah inklusi ramah anak",
        query_vector=vector,
        size=5
    )
    assert len(hits) > 0
    assert all("_score" in h for h in hits)

@pytest.mark.unit
def test_should_use_hybrid_semantic_query():
    """Query semantik harus trigger hybrid path."""
    intent = CognitiveIntent(module="list", raw_label="sekolah inklusi di Jakarta")
    assert should_use_hybrid(intent) == True

@pytest.mark.unit  
def test_should_use_hybrid_specific_filter_no_hybrid():
    """Query dengan filter spesifik tidak perlu hybrid."""
    intent = CognitiveIntent(
        module="list", 
        jenjang="SMK",
        wilayah=WilayahEntities(kabupaten="bandung")
    )
    assert should_use_hybrid(intent) == False
```

### 6.2 Evaluation Metrics

| Metric | Target | Cara Ukur |
|--------|--------|-----------|
| Recall@10 | ≥ 0.85 | Dari evaluation set manual (50 query semantik) |
| MRR@10 | ≥ 0.70 | Mean Reciprocal Rank dari evaluation set |
| Latency P95 | < 800ms | Tambahan ~200-300ms vs pure BM25 (embedding + kNN) |
| Precision@5 | ≥ 0.75 | Relevance judgment manual |

**Evaluation set yang perlu dibuat (oleh tim):**
- 50 query semantik dengan ground truth dokumen relevan
- Contoh: "sekolah inklusi di Bandung" → daftar NPSN sekolah inklusi yang benar
- Simpan di `tests/fixtures/semantic_eval_set.json`

### 6.3 Regression Guard untuk Existing Path

```python
# Tambah ke test_regression.py setelah hybrid implementasi:

@pytest.mark.unit
def test_aggregation_query_does_not_trigger_hybrid():
    """Aggregation query TIDAK boleh masuk hybrid path."""
    intent = CognitiveIntent(module="aggregation", jenjang="SMK")
    assert should_use_hybrid(intent) == False

@pytest.mark.unit
def test_list_with_wilayah_does_not_trigger_hybrid():
    """List query dengan wilayah spesifik tidak perlu hybrid."""
    intent = CognitiveIntent(
        module="list",
        wilayah=WilayahEntities(provinsi="jawa barat")
    )
    assert should_use_hybrid(intent) == False
```

---

## 7. Migration Plan

### 7.1 Phased Rollout

```
Phase 0 (sekarang): Keyword-only
├── Semua query → BM25
└── content_vector field ada tapi index=false

Phase 1 (post-vektorisasi): Reindex + Enable kNN
├── Buat idx_sekolah_qc_v2 dengan index=true
├── Populate vectors (batch process ~3-4 jam)
├── Alias swap (zero downtime)
└── Masih keyword-only di application layer

Phase 2 (A/B test): Shadow Mode
├── Jalankan hybrid search paralel dengan BM25 (tidak di-serve ke user)
├── Log hasil hybrid vs BM25 untuk evaluasi
├── Hitung Recall@10 dan MRR@10 dari shadow traffic
└── Threshold: hybrid ≥ BM25 quality sebelum promote

Phase 3 (production): Hybrid untuk Semantic Queries
├── Enable hybrid path untuk query dengan is_semantic=True
├── Monitor latency (target < 800ms P95)
├── Monitor quality score dari confidence_engine
└── Gradual rollout: 10% → 50% → 100% semantic queries

Phase 4 (optional): Expand Hybrid Coverage
└── Evaluasi apakah list handler juga benefit dari hybrid
```

### 7.2 Rollback Strategy

Rollback dapat dilakukan dalam < 5 menit:

```python
# Feature flag di config atau env var
HYBRID_SEARCH_ENABLED = os.getenv("HYBRID_SEARCH_ENABLED", "false").lower() == "true"

# Di main.py
if HYBRID_SEARCH_ENABLED and should_use_hybrid(intent):
    hits = await perform_hybrid_search(...)
else:
    hits = await perform_search(...)  # fallback ke BM25
```

Jika terjadi degradasi:
1. Set `HYBRID_SEARCH_ENABLED=false` di environment → immediate rollback tanpa deploy
2. Alias ES bisa di-revert ke index lama jika ada masalah di level mapping

### 7.3 Monitoring Metrics (Production)

Tambahkan ke structured logging (sudah ada `_log()` helper dari Priority 4A):

```python
_log(logger, "info", "search_executed", 
     mode="hybrid",           # atau "keyword"
     query_len=len(query),
     num_hits=len(hits),
     latency_ms=elapsed,
     top_score=hits[0]["_score"] if hits else 0,
     location_filter=bool(location_filter))
```

Dashboard metrics yang perlu dimonitor:
- `search_executed.mode=hybrid` count vs `search_executed.mode=keyword`
- P50/P95/P99 latency per mode
- `num_hits=0` rate per mode (zero-result rate)
- Confidence score distribution post-hybrid

---

## Appendix: Dependency & Prerequisites Checklist

Sebelum implementasi Phase 1 bisa dimulai:

- [ ] **Rekan vektorisasi:** Install `sentence-transformers` di environment
- [ ] **Rekan vektorisasi:** Test model `paraphrase-multilingual-MiniLM-L12-v2` dengan sample content field
- [ ] **Rekan vektorisasi:** Estimasi storage tambahan (384 dims × 4 bytes × 562k docs ≈ **865MB** tambahan)
- [ ] **Rekan vektorisasi:** Setup checkpoint mechanism sebelum mulai batch process
- [ ] **Dev:** Confirm ES version ≥ 8.9 (untuk native RRF support)
- [ ] **Dev:** Buat `idx_sekolah_qc_v2` dengan mapping baru sebelum vectorization dimulai
- [ ] **Dev:** Siapkan evaluation set 50 query semantik dengan ground truth
- [ ] **Ops:** Pastikan ES heap ≥ 4GB untuk accommodate HNSW index (~1.2GB tambahan)
- [ ] **Ops:** Set `refresh_interval: -1` di index sebelum batch vectorization dimulai

---

*Dokumen ini adalah living document — update sesuai temuan saat implementasi.*  
*Referensi: [ES kNN search](https://www.elastic.co/guide/en/elasticsearch/reference/current/knn-search.html) | [ES RRF](https://www.elastic.co/guide/en/elasticsearch/reference/current/rrf.html) | [sentence-transformers](https://www.sbert.net/)*
