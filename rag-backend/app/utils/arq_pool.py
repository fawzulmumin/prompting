"""Singleton ArqRedis connection pool."""
import os
import logging
from typing import Optional
from arq.connections import ArqRedis, create_pool, RedisSettings

logger = logging.getLogger(__name__)

_pool: Optional[ArqRedis] = None


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _pool = await create_pool(RedisSettings.from_dsn(redis_url))
        logger.info("[ARQ] Pool created")
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        _pool = None
        logger.info("[ARQ] Pool closed")
