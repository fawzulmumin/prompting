"""
TieredCache: L1 (in-memory LRU, TTL pendek) + L2 (Redis, TTL panjang).

Keunggulan vs get_cache/set_cache biasa:
- L1 hit: ~0ms (tidak perlu network round-trip ke Redis)
- L2 hit: ~1-3ms (Redis local network)
- Miss: query ke ES/LLM seperti biasa

Desain:
- L1: dict Python dengan maxsize dan per-entry TTL (implementasi manual, tanpa lib tambahan)
- L2: Redis via get_cache/set_cache yang sudah ada
- API: async get(key), async set(key, value, ttl_l1, ttl_l2)
- Eviction L1: LRU sederhana — saat maxsize tercapai, hapus entry terlama
- Thread-safe: pakai asyncio.Lock
"""

import asyncio
import time
from typing import Any, Optional

from app.utils.cache import get_cache, set_cache


class TieredCache:
    def __init__(self, maxsize: int = 256):
        self._l1: dict = {}          # key → (value, expire_at)
        self._order: list = []       # LRU order: oldest di depan, newest di belakang
        self._maxsize = maxsize
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            entry = self._l1.get(key)
            if entry is not None:
                value, expire_at = entry
                if time.monotonic() < expire_at:
                    # cache hit — pindahkan ke akhir (most recently used)
                    self._order.remove(key)
                    self._order.append(key)
                    return value
                # expired: hapus dari L1
                del self._l1[key]
                self._order.remove(key)

        # L1 miss → cek L2 (Redis)
        result = await get_cache(key)
        if result is not None:
            # populate L1 dengan TTL default pendek (60s)
            await self._set_l1(key, result, ttl=60)
        return result

    async def set(
        self,
        key: str,
        value: Any,
        ttl_l1: int = 60,
        ttl_l2: int = 600,
    ) -> None:
        await self._set_l1(key, value, ttl_l1)
        await set_cache(key, value, ttl=ttl_l2)

    async def _set_l1(self, key: str, value: Any, ttl: int) -> None:
        async with self._lock:
            if key in self._l1:
                self._order.remove(key)
            elif len(self._l1) >= self._maxsize:
                # evict LRU (entry paling lama)
                oldest = self._order.pop(0)
                self._l1.pop(oldest, None)
            expire_at = time.monotonic() + ttl if ttl > 0 else 0.0
            self._l1[key] = (value, expire_at)
            self._order.append(key)

    async def invalidate(self, key: str) -> None:
        async with self._lock:
            self._l1.pop(key, None)
            if key in self._order:
                self._order.remove(key)

    def stats(self) -> dict:
        return {"l1_size": len(self._l1), "l1_maxsize": self._maxsize}
