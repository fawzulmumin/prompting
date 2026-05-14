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
import re as _re

logger = logging.getLogger(__name__)

# =========================
# HELPERS
# =========================
def _log(logger_instance, level: str, event: str, **kwargs):
    """Emit single-line JSON log. Fallback ke plain log jika json gagal."""
    import json as _j, time
    try:
        payload = {"event": event, "ts": round(time.time(), 3), **kwargs}
        msg = _j.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        msg = f"{event} {kwargs}"
    getattr(logger_instance, level)(msg)


# ---------------------------------------------------------------------------
# Normalization tables — extend as needed, no code change required
# ---------------------------------------------------------------------------
_ALIAS_MAP: dict[str, str] = {
    # Wilayah alias
    "tangsel": "tangerang selatan",
    "jakpus": "jakarta pusat",
    "jaksel": "jakarta selatan",
    "jakbar": "jakarta barat",
    "jaktim": "jakarta timur",
    "jakut": "jakarta utara",
    "dki": "dki jakarta",
    "jogja": "yogyakarta",
    "yogya": "yogyakarta",
    "diy": "yogyakarta",
    "solo": "surakarta",
    "jabar": "jawa barat",
    "jatim": "jawa timur",
    "jateng": "jawa tengah",
    "sulsel": "sulawesi selatan",
    "sulut": "sulawesi utara",
    "sulteng": "sulawesi tengah",
    "sulbar": "sulawesi barat",
    "sultra": "sulawesi tenggara",
    "sumut": "sumatera utara",
    "sumbar": "sumatera barat",
    "sumsel": "sumatera selatan",
    "kalsel": "kalimantan selatan",
    "kalbar": "kalimantan barat",
    "kaltim": "kalimantan timur",
    "kalteng": "kalimantan tengah",
    "kaltara": "kalimantan utara",
    "ntt": "nusa tenggara timur",
    "ntb": "nusa tenggara barat",
    # Jenjang alias (nama panjang → singkatan)
    "sekolah dasar": "sd",
    "sekolah menengah pertama": "smp",
    "sekolah menengah atas": "sma",
    "sekolah menengah kejuruan": "smk",
    "madrasah ibtidaiyah": "mi",
    "madrasah tsanawiyah": "mts",
    "madrasah aliyah": "ma",
    "madrasah aliyah negeri": "man",
    "sekolah luar biasa": "slb",
    "taman kanak": "tk",
    "pendidikan anak usia dini": "paud",
    "pusat kegiatan belajar masyarakat": "pkbm",
    # Typo umum
    "propinsi": "provinsi",
    "brapa": "berapa",
    "skolah": "sekolah",
    "skola": "sekolah",
    "seklah": "sekolah",
}

_STOPWORDS: frozenset[str] = frozenset({
    # Query filler
    "berapa", "jumlah", "total", "ada", "tolong", "cari",
    "tampilkan", "lihat", "tunjukkan", "kasih", "tahu", "info",
    "informasi", "data", "daftar", "list", "show", "get",
    # Preposisi / konjungsi
    "di", "dari", "ke", "pada", "untuk", "dan", "atau",
    "yang", "dengan", "dalam", "tentang", "adalah", "apa", "saja",
    "semua", "seluruh", "setiap",
    # Location prefix (di-normalize terpisah, tapi backup di sini)
    "provinsi", "kabupaten", "kota", "kecamatan", "kelurahan", "desa",
    "prov", "kab", "kec",
})

_LOCATION_PREFIX_PATTERNS: list[tuple[str, str]] = [
    # Urutan penting: lebih spesifik dulu
    (r"\bkecamatan\b", ""),
    (r"\bkelurahan\b", ""),
    (r"\bkabupaten\b", ""),
    (r"\bkota\b", ""),
    (r"\bprovinsi\b", ""),
    (r"\bprov\b", ""),
    (r"\bkab\b\.?\s*", ""),
    (r"\bkec\b\.?\s*", ""),
]


def _normalize_cache_key(raw: str) -> str:
    """
    Normalize a raw user query into a canonical cache key.

    Pipeline:
    1. MD5 hash passthrough (backward compat)
    2. Lowercase + strip + remove punctuation
    3. Apply multi-word alias map (longest match first)
    4. Apply location prefix removal
    5. Tokenize + single-word alias map + typo correction
    6. Remove stopwords
    7. Sort tokens (word-order invariant)
    8. Rejoin

    Pure function — no I/O, no side effects, <1ms.
    """
    if len(raw) == 32 and all(c in "0123456789abcdef" for c in raw):
        return raw  # sudah hash MD5, jangan diproses

    text = raw.lower().strip()

    # Remove punctuation
    text = _re.sub(r"[^\w\s]", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()

    # Apply multi-word aliases (longest match first to avoid partial replacement)
    multi_word = sorted(
        [(k, v) for k, v in _ALIAS_MAP.items() if " " in k],
        key=lambda x: -len(x[0]),
    )
    for alias, canonical in multi_word:
        text = _re.sub(r"\b" + _re.escape(alias) + r"\b", canonical, text)

    # Remove location prefixes
    for pattern, replacement in _LOCATION_PREFIX_PATTERNS:
        text = _re.sub(pattern, replacement, text)
    text = _re.sub(r"\s+", " ", text).strip()

    # Tokenize, apply single-word aliases, remove stopwords
    # Note: alias may expand to multi-word (e.g. "tangsel" → "tangerang selatan")
    # so split sub-tokens and process each independently.
    result_tokens = []
    for token in text.split():
        expanded = _ALIAS_MAP.get(token, token)
        sub_tokens = expanded.split() if " " in expanded else [expanded]
        for st in sub_tokens:
            if st in _STOPWORDS or len(st) < 2:
                continue
            result_tokens.append(st)

    # Deduplicate (alias expansion can produce duplicates, e.g. "dki jakarta" + expanded "dki")
    # then sort for word-order invariance
    return " ".join(sorted(set(result_tokens)))


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
            _log(logger, "info", "redis_connecting", url=str(REDIS_URL)[:40])
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
                _log(logger, "info", "redis_connected")
            except Exception as e:
                _log(logger, "error", "redis_connect_failed", error=str(e))

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

    normalized_key = _normalize_cache_key(key)
    if normalized_key != key:
        _log(logger, "debug", "cache_key_normalized", original=key[:40], normalized=normalized_key[:40])
    key = normalized_key

    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return None

        full_key = f"{CACHE_PREFIX}{key}"
        data = await redis_client.get(full_key)

        if data:
            try:
                result = json.loads(data)
                _log(logger, "debug", "cache_hit", key=key[:40])
                return result
            except json.JSONDecodeError as e:
                _log(logger, "warning", "cache_decode_error", key=key[:40], error=str(e))
                return None
        _log(logger, "debug", "cache_miss", key=key[:40])
        return None

    except (ConnectionError, TimeoutError):
        _log(logger, "warning", "redis_disabled")
        REDIS_AVAILABLE = False
        return None
    except Exception:
        _log(logger, "warning", "cache_get_unexpected_error")
        return None

async def set_cache(key: str, value: Any, ttl: int = 3600) -> bool:
    global REDIS_AVAILABLE

    if not key or value is None or not REDIS_AVAILABLE:
        return False

    normalized_key = _normalize_cache_key(key)
    if normalized_key != key:
        _log(logger, "debug", "cache_key_normalized", original=key[:40], normalized=normalized_key[:40])
    key = normalized_key

    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return False

        full_key = f"{CACHE_PREFIX}{key}"

        # Safe JSON serialization
        string_data = json.dumps(value, ensure_ascii=False, default=str)
        await redis_client.setex(full_key, ttl, string_data)
        _log(logger, "info", "cache_set", key=key[:40], ttl=ttl, size_bytes=len(string_data))
        return True

    except (ConnectionError, TimeoutError):
        _log(logger, "warning", "redis_disabled")
        REDIS_AVAILABLE = False
        return False
    except Exception:
        _log(logger, "warning", "cache_set_unexpected_error")
        return False

async def delete_cache(key: str) -> bool:
    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return False
        full_key = f"{CACHE_PREFIX}{key}"
        result = await redis_client.delete(full_key)
        return result > 0
    except Exception as e:
        _log(logger, "error", "cache_delete_failed", key=key[:40], error=str(e))
        return False

async def clear_cache_pattern(pattern: str, batch_size: int = 100) -> int:
    """Safely clear cache keys in batches (production safe)."""
    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return 0
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
        _log(logger, "error", "cache_clear_pattern_failed", pattern=pattern[:40], error=str(e))
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
        if redis_client is None:
            return False
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
        _log(logger, "error", "chat_history_save_failed", error=str(e))
        return False

async def get_chat_history(user_id: str) -> List[Dict[str, Any]]:
    if not user_id:
        return []

    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return []
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
        _log(logger, "error", "chat_history_get_failed", error=str(e))
        return []

async def clear_chat_history(user_id: str) -> bool:
    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return False
        key = f"{CACHE_PREFIX}history:{user_id}"
        result = await redis_client.delete(key)
        return result > 0
    except Exception as e:
        _log(logger, "error", "chat_history_clear_failed", error=str(e))
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
        _log(logger, "debug", "counter_increment", key=key[:40])
        return int(value)
    except Exception:
        _log(logger, "warning", "redis_disabled")
        REDIS_AVAILABLE = False
        return 0

async def get_counter(key: str) -> int:
    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return 0
        full_key = f"{CACHE_PREFIX}{key}"
        value = await redis_client.get(full_key)
        return int(value) if value else 0
    except Exception as e:
        _log(logger, "error", "counter_get_failed", key=key[:40], error=str(e))
        return 0

# =========================
# HEALTH CHECK
# =========================
async def check_redis_health() -> Dict[str, Any]:
    try:
        redis_client = await get_redis_client()
        if redis_client is None:
            return {"status": "ERROR", "connected": False, "error": "Redis unavailable"}
        await redis_client.ping()

        info = await redis_client.info("memory")
        memory_used = info.get("used_memory_human", "unknown")

        db_info = await redis_client.info("keyspace")
        key_count = sum(v.get("keys", 0) for v in db_info.values() if isinstance(v, dict))

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
]
