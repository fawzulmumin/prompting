# utils/cache.py
# Production-ready Redis cache layer
import app.config as config # Ensure environment variables are loaded
import os
import json
import hashlib
import logging
import asyncio
from typing import Any, Optional, List, Dict
import redis.asyncio as redis
from redis.exceptions import ConnectionError, TimeoutError

logger = logging.getLogger(__name__)

# =========================
# CONFIGURATION
# =========================
CACHE_PREFIX = "rag_edu_v1:"
REDIS_URL = os.getenv("REDIS_URL")
REDIS_AVAILABLE = True
# =========================
# REDIS CONNECTION (ASYNC SINGLETON)
# =========================
_redis_client: Optional[redis.Redis] = None
_redis_lock = asyncio.Lock()

async def get_redis_client() -> redis.Redis:
    """Get or create Redis client (async singleton, thread-safe)."""
    global _redis_client, REDIS_AVAILABLE

    if not REDIS_AVAILABLE:
        return None
    
    async with _redis_lock:
        if _redis_client is None:
            logger.info(f"Connecting to Redis at {REDIS_URL}...")
            try:
                _redis_client = redis.from_url(
                    REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                    retry_on_timeout=True,
                    max_connections=50,
                    health_check_interval=30,
                )
                # Async ping
                await _redis_client.ping()
                logger.info("✓ Redis connection established")
            except Exception as e:
                logger.error(f"Redis disabled due to connection failure")
                logger.error(f"✗ Failed to connect to Redis: {e}")
                
                REDIS_AVAILABLE = False
                _redis_client = None
                return None

    return _redis_client

# =========================
# CACHE OPERATIONS
# =========================
async def get_cache(key: str) -> Optional[Any]:
    global REDIS_AVAILABLE
    
    if not key or not REDIS_AVAILABLE:
        return None

    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return None
        
        full_key = f"{CACHE_PREFIX}{key}"
        data = await redis_client.get(full_key)

        if data:
            try:
                return json.loads(data)
            except json.JSONDecodeError as e:
                logger.warning(f"Cache decode error for key '{key}': {e}")
                return None
        return None

    except (ConnectionError, TimeoutError):
        logger.warning("Redis disabled due to runtime failure")
        REDIS_AVAILABLE = False
        return None
    except Exception:
        logger.warning("Unexpected error in get_cache")
        return None

async def set_cache(key: str, value: Any, ttl: int = 3600) -> bool:
    global REDIS_AVAILABLE
    
    if not key or value is None or not REDIS_AVAILABLE:
        return False

    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return False
        
        full_key = f"{CACHE_PREFIX}{key}"

        # Safe JSON serialization
        string_data = json.dumps(value, ensure_ascii=False, default=str)
        await redis_client.setex(full_key, ttl, string_data)
        logger.info(
    f"[Cache] SET key={full_key} ttl={ttl} size={len(string_data)} bytes"
)

        return True

    except (ConnectionError, TimeoutError):
        logger.warning("Redis disabled due to runtime failure")
        REDIS_AVAILABLE = False
        return False
    except Exception:
        logger.warning("Unexpected error in set_cache")
        return False

async def delete_cache(key: str) -> bool:
    try:
        redis_client = await get_redis_client()
        full_key = f"{CACHE_PREFIX}{key}"
        result = await redis_client.delete(full_key)
        return result > 0
    except Exception as e:
        logger.error(f"Error deleting cache key '{key}': {e}")
        return False

async def clear_cache_pattern(pattern: str, batch_size: int = 100) -> int:
    """Safely clear cache keys in batches (production safe)."""
    try:
        redis_client = await get_redis_client()
        full_pattern = f"{CACHE_PREFIX}{pattern}"

        cursor = 0
        deleted_count = 0

        while True:
            cursor, keys = await redis_client.scan(cursor, match=full_pattern, count=batch_size)
            if keys:
                # batch delete
                for i in range(0, len(keys), batch_size):
                    batch = keys[i:i+batch_size]
                    deleted = await redis_client.delete(*batch)
                    deleted_count += deleted

            if cursor == 0:
                break

        return deleted_count

    except Exception as e:
        logger.error(f"Error clearing cache pattern '{pattern}': {e}")
        return 0

# =========================
# CHAT HISTORY
# =========================
async def save_chat_history(
    user_id: str,
    message_role: str,
    message_content: str,
    max_history: int = 10,
    ttl: int = 86400,
) -> bool:

    if not user_id:
        return False

    try:
        redis_client = await get_redis_client()
        key = f"{CACHE_PREFIX}history:{user_id}"

        message = {
            "role": message_role,
            "content": str(message_content),
            "timestamp": int(__import__('time').time() * 1000)
        }

        message_json = json.dumps(message, ensure_ascii=False, default=str)

        pipeline = redis_client.pipeline()
        pipeline.rpush(key, message_json)
        pipeline.ltrim(key, -max_history, -1)
        pipeline.expire(key, ttl)
        await pipeline.execute()

        return True

    except Exception as e:
        logger.error(f"Error saving chat history: {e}", exc_info=True)
        return False

async def get_chat_history(user_id: str) -> List[Dict[str, Any]]:
    if not user_id:
        return []

    try:
        redis_client = await get_redis_client()
        key = f"{CACHE_PREFIX}history:{user_id}"
        history = await redis_client.lrange(key, 0, -1)

        parsed = []
        for msg in history:
            try:
                parsed.append(json.loads(msg))
            except Exception:
                continue
        return parsed

    except Exception as e:
        logger.error(f"Error getting chat history: {e}", exc_info=True)
        return []

async def clear_chat_history(user_id: str) -> bool:
    try:
        redis_client = await get_redis_client()
        key = f"{CACHE_PREFIX}history:{user_id}"
        result = await redis_client.delete(key)
        return result > 0
    except Exception as e:
        logger.error(f"Error clearing chat history: {e}")
        return False

# =========================
# HASHING
# =========================
def hash_prompt(prompt: str) -> str:
    if not prompt:
        return "default_hash"
    return hashlib.md5(str(prompt).encode("utf-8")).hexdigest()

def hash_key(*args) -> str:
    combined = "".join(str(a) for a in args)
    return hashlib.md5(combined.encode("utf-8")).hexdigest()

# =========================
# RATE LIMITING
# =========================
async def increment_counter(key: str, ttl: int = 60) -> int:
    global REDIS_AVAILABLE
    
    if not key or not REDIS_AVAILABLE:
        return 0
    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return 0
        full_key = f"{CACHE_PREFIX}{key}"
        value = await redis_client.incr(full_key)
        if value == 1:
            await redis_client.expire(full_key, ttl)
        return int(value)
    except Exception:
        logger.warning("Redis disabled due to runtime failure")
        REDIS_AVAILABLE = False
        return 0

async def get_counter(key: str) -> int:
    try:
        redis_client = await get_redis_client()
        full_key = f"{CACHE_PREFIX}{key}"
        value = await redis_client.get(full_key)
        return int(value) if value else 0
    except Exception as e:
        logger.error(f"Error getting counter '{key}': {e}")
        return 0

# =========================
# HEALTH CHECK
# =========================
async def check_redis_health() -> Dict[str, Any]:
    try:
        redis_client = await get_redis_client()
        await redis_client.ping()

        info = await redis_client.info("memory")
        memory_used = info.get("used_memory_human", "unknown")

        cursor = 0
        key_count = 0
        while True:
            cursor, keys = await redis_client.scan(cursor, match=f"{CACHE_PREFIX}*", count=100)
            key_count += len(keys)
            if cursor == 0:
                break

        return {
            "status": "OK",
            "connected": True,
            "memory_used": memory_used,
            "total_keys": key_count,
            "prefix": CACHE_PREFIX,
        }

    except Exception as e:
        return {
            "status": "ERROR",
            "connected": False,
            "error": str(e),
        }

# =========================
# CONTEXT MANAGER
# =========================
class RedisCacheManager:
    def __init__(self):
        self.client = None

    async def __aenter__(self):
        self.client = await get_redis_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def get(self, key: str) -> Optional[Any]:
        return await get_cache(key)

    async def set(self, key: str, value: Any, ttl: int = 3600) -> bool:
        return await set_cache(key, value, ttl)

    async def delete(self, key: str) -> bool:
        return await delete_cache(key)

# =========================
# EXPORT API
# =========================
__all__ = [
    "get_cache",
    "set_cache",
    "delete_cache",
    "clear_cache_pattern",
    "save_chat_history",
    "get_chat_history",
    "clear_chat_history",
    "hash_prompt",
    "hash_key",
    "increment_counter",
    "get_counter",
    "check_redis_health",
    "RedisCacheManager",
]
