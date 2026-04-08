import sys
import os
from pathlib import Path
from dotenv import load_dotenv # Tambahkan ini!
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer
import torch

# =====================================================
# 1. Load Environment Variables
# =====================================================
BASE_DIR = Path(__file__).resolve().parent.parent
# Cek apakah kita sedang di mode PROD
is_prod = os.getenv("PROD") == "production"
ENV_FILE = BASE_DIR / (".env.production" if is_prod else ".env")

if not load_dotenv(ENV_FILE):
    print(f"❌ Gagal memuat file env di: {ENV_FILE}")
    sys.exit(1)

# Ambil variabel dari .env
ES_URL = os.getenv("ELASTIC_URL") or os.getenv("ELASTICSEARCH_NODE")
ES_USER = os.getenv("ELASTIC_USER") or os.getenv("ELASTICSEARCH_USERNAME")
ES_PASS = os.getenv("ELASTIC_PASSWORD") or os.getenv("ELASTICSEARCH_PASSWORD")
INDEX_NAME = os.getenv("ELASTIC_INDEX") or "idx_prompting_agent"

# Cek apakah URL ditemukan
if not ES_URL:
    print("❌ ELASTICSEARCH_NODE tidak ditemukan di .env")
    sys.exit(1)

# =====================================================
# 2. Elasticsearch Connection
# =====================================================
es = Elasticsearch(
    hosts=[ES_URL], # Sekarang dipastikan berupa string URL
    basic_auth=(ES_USER, ES_PASS),
    verify_certs=False, # Gunakan jika pakai self-signed certificate
    request_timeout=60
)

# =====================================================
# 3. Load Embedding Model
# =====================================================
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"🚀 Embedding device: {device}")

model = SentenceTransformer("all-MiniLM-L6-v2", device=device)

# =====================================================
# 4. Search Function
# =====================================================
def semantic_search(text_query: str, top_k: int = 5):
    # Buat vektor
    vector_query = model.encode(text_query, normalize_embeddings=True).tolist()

    try:
        response = es.search(
            index=INDEX_NAME,
            knn={
                "field": "content_vector", # Pastikan ini sama dengan mapping
                "query_vector": vector_query,
                "k": top_k,
                "num_candidates": 100
            },
            _source=["nama", "kecamatan", "kabupaten", "provinsi"]
        )

        print(f"\n🔍 Hasil untuk: '{text_query}'\n")
        for hit in response["hits"]["hits"]:
            src = hit["_source"]
            score = hit["_score"]
            print(f"[{score:.4f}] {src.get('nama')} ({src.get('kecamatan')}, {src.get('kabupaten')})")
            
    except Exception as e:
        print(f"❌ Error saat pencarian: {e}")

if __name__ == "__main__":
    semantic_search("Sekolah yang lokasinya dekat dengan area perhutanan atau perkebunan")