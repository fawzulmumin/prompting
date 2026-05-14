import os
import logging
import asyncio
from typing import List, Optional

from sentence_transformers import SentenceTransformer

from app.utils.tiered_cache import TieredCache

logger = logging.getLogger(__name__)
EMBEDDING_ENABLED = os.getenv("EMBEDDING_ENABLED", "true").lower() == "true"
# =========================================================
# CONFIG
# =========================================================

MODEL_NAME = "./models/all-MiniLM-L6-v2"
EMBED_TIMEOUT_SECONDS = 15

# =========================================================
# SINGLETON
# =========================================================

_embedding_model: Optional[SentenceTransformer] = None

# Embedding vector cache: L1 in-memory (maxsize=512, TTL 5 menit) + L2 Redis (TTL 24 jam).
# Singleton — dibuat sekali saat module di-import, dipakai semua call.
_embedding_cache = TieredCache(maxsize=512)


def get_embedding_model() -> SentenceTransformer:
    """
    Lazy-load singleton embedding model.
    Safe against double initialization.
    """
    if not EMBEDDING_ENABLED:
        logger.info("Embedding disabled via environment flag")
        return None

    global _embedding_model

    if _embedding_model is not None:
        return _embedding_model

    try:
        logger.info("Loading embedding model from %s", MODEL_NAME)
        _embedding_model = SentenceTransformer(
            MODEL_NAME,
            local_files_only=True  
        )
        logger.info("Embedding model ready")
        return _embedding_model

    except Exception as e:
        logger.error("Embedding model failed to load: %s", e)
        _embedding_model = None
        return None


# =========================================================
# PUBLIC API
# =========================================================

async def get_embedding(text: str) -> List[float]:
    """
    Generate embedding for a single text.
    """

    if not text or not text.strip():
        logger.warning("Empty text received for embedding")
        return []

    model = get_embedding_model()

    if model is None:
        logger.warning("Embedding disabled or unavailable")
        return []
    
    loop = asyncio.get_running_loop()

    try:
        embedding = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: model.encode(
                    text,
                    convert_to_tensor=False,
                    normalize_embeddings=True
                )
            ),
            timeout=EMBED_TIMEOUT_SECONDS
        )

        if hasattr(embedding, "tolist"):
            return embedding.tolist()

        return list(embedding)

    except asyncio.TimeoutError:
        logger.error("Embedding timeout")
        return []

    except Exception:
        logger.exception("Embedding generation failed")
        return []


async def get_embeddings(texts: List[str]) -> List[List[float]]:
    """
    Batch embedding support (future-proof).
    """

    if not texts:
        return []

    model = get_embedding_model()
    if model is None:
        logger.warning("Embedding disabled or unavailable")
        return []
    loop = asyncio.get_running_loop()

    try:
        embeddings = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: model.encode(
                    texts,
                    convert_to_tensor=False,
                    normalize_embeddings=True,
                    batch_size=64
                )
            ),
            timeout=EMBED_TIMEOUT_SECONDS
        )

        return embeddings.tolist()

    except asyncio.TimeoutError:
        logger.error("Batch embedding timeout")
        return []

    except Exception:
        logger.exception("Batch embedding failed")
        return []


async def get_cached_embedding(text: str) -> List[float]:
    """
    Return embedding vector untuk text, dengan TieredCache L1+L2.

    Cache hierarchy:
    - L1: in-memory LRU, maxsize=512, TTL 5 menit  (~0ms)
    - L2: Redis, TTL 24 jam                          (~1-3ms)
    - Miss: generate via SentenceTransformer + simpan ke L1+L2

    Key: "embed:{md5(text)}" — konsisten dengan implementasi sebelumnya
    untuk backward compatibility dengan Redis keys yang sudah ada.
    """
    import hashlib as _hashlib

    if not text or not text.strip():
        return []

    cache_key = f"embed:{_hashlib.md5(text.encode()).hexdigest()}"

    try:
        cached = await _embedding_cache.get(cache_key)
        if cached:
            logger.debug("[EMBED] cache hit (L1/L2): key=%s", cache_key[:20])
            return cached
    except Exception:
        logger.warning("[EMBED] cache get failed, falling through to generate")

    vector = await get_embedding(text)

    if vector:
        try:
            await _embedding_cache.set(
                cache_key,
                vector,
                ttl_l1=300,   # 5 menit di L1
                ttl_l2=86400, # 24 jam di L2 (Redis)
            )
            logger.debug("[EMBED] cached to L1+L2: key=%s", cache_key[:20])
        except Exception:
            logger.warning("[EMBED] cache set failed, continuing without cache")

    return vector


def get_embedding_cache_stats() -> dict:
    """Return L1 cache stats untuk monitoring/health endpoint."""
    return _embedding_cache.stats()
