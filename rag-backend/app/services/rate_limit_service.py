import os
import logging
from typing import Dict, Any

from redis.asyncio import Redis
from app.utils import cache

logger = logging.getLogger(__name__)

# ======================================================
# CONFIG
# ======================================================

RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", 5))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", 60))
FAIL_OPEN = os.getenv("RATE_LIMIT_FAIL_OPEN", "true").lower() == "true"

async def check_rate_limit(user_id: str) -> Dict[str, Any]:
    """
    Atomic rate limit using Redis INCR + EXPIRE.
    Sliding window approximation.
    """

    if not user_id:
        return _allow_without_limit("missing_user_id")

    redis = await cache.get_redis_client()

    if redis is None:
        return _handle_redis_unavailable()

    key = f"rag_edu_v2:ratelimit:{user_id}"

    try:
        current = await redis.incr(key)

        if current == 1:
            await redis.expire(key, RATE_LIMIT_WINDOW)

        ttl = await redis.ttl(key)
        ttl = max(ttl, 0)

    except Exception as e:
        logger.error("[RATE_LIMIT] Redis error: %s", str(e))
        return _handle_redis_unavailable()

    if current > RATE_LIMIT_REQUESTS:
        logger.warning(
            "[RATE_LIMIT] Blocked user=%s current=%s",
            user_id,
            current
        )

        return {
            "allowed": False,
            "remaining": 0,
            "reset_in": ttl
        }

    return {
        "allowed": True,
        "remaining": RATE_LIMIT_REQUESTS - current,
        "reset_in": ttl
    }

def _handle_redis_unavailable() -> Dict[str, Any]:
    if FAIL_OPEN:
        logger.warning("[RATE_LIMIT] Redis unavailable - fail open")
        return {
            "allowed": True,
            "remaining": RATE_LIMIT_REQUESTS,
            "reset_in": RATE_LIMIT_WINDOW
        }

    logger.error("[RATE_LIMIT] Redis unavailable - fail closed")
    return {
        "allowed": False,
        "remaining": 0,
        "reset_in": RATE_LIMIT_WINDOW
    }


def _allow_without_limit(reason: str) -> Dict[str, Any]:
    logger.warning("[RATE_LIMIT] Allow without limit: %s", reason)
    return {
        "allowed": True,
        "remaining": RATE_LIMIT_REQUESTS,
        "reset_in": RATE_LIMIT_WINDOW
    }
