import os
import logging
import asyncio
from typing import List, Optional

from sentence_transformers import SentenceTransformer

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
    """Return embedding with Redis cache (key: embed:md5(text), TTL 3600)."""
    import hashlib as _hashlib
    from app.utils.cache import get_cache, set_cache
    cache_key = f"embed:{_hashlib.md5(text.encode()).hexdigest()}"
    try:
        cached = await get_cache(cache_key)
        if cached:
            return cached
    except Exception:
        pass
    vector = await get_embedding(text)
    try:
        if vector:
            await set_cache(cache_key, vector, ttl=3600)
    except Exception:
        pass
    return vector
