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
        # Hit/miss counters — in-memory only, reset to 0 on every restart.
        # Integer += in asyncio single-threaded context is safe without extra lock
        # (no coroutine context switch between Python bytecodes without explicit await).
        self._total_requests: int = 0
        self._l1_hits: int = 0
        self._l1_misses: int = 0   # L1 miss (termasuk yang L2 hit)
        self._l2_hits: int = 0
        self._l2_misses: int = 0   # full miss (L1 miss + L2 miss)

    async def get(self, key: str) -> Optional[Any]:
        self._total_requests += 1
        async with self._lock:
            entry = self._l1.get(key)
            if entry is not None:
                value, expire_at = entry
                if time.monotonic() < expire_at:
                    # L1 hit — pindahkan ke akhir (most recently used)
                    self._l1_hits += 1
                    self._order.remove(key)
                    self._order.append(key)
                    return value
                # expired: hapus dari L1, jatuh ke L1 miss
                del self._l1[key]
                self._order.remove(key)

        # L1 miss → cek L2 (Redis)
        self._l1_misses += 1
        result = await get_cache(key)
        if result is not None:
            self._l2_hits += 1
            # populate L1 dengan TTL default pendek (60s)
            await self._set_l1(key, result, ttl=60)
        else:
            self._l2_misses += 1
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
        total = self._total_requests
        l2_requests = self._l1_misses
        l1_hit_rate = round(self._l1_hits / total, 4) if total > 0 else 0.0
        l2_hit_rate = round(self._l2_hits / l2_requests, 4) if l2_requests > 0 else 0.0
        overall_hit_rate = round((self._l1_hits + self._l2_hits) / total, 4) if total > 0 else 0.0
        return {
            "l1_size": len(self._l1),
            "l1_maxsize": self._maxsize,
            "l2_backend": "redis",
            "total_requests": total,
            "l1_hits": self._l1_hits,
            "l2_hits": self._l2_hits,
            "l1_misses": self._l1_misses,
            "l2_misses": self._l2_misses,
            "l1_hit_rate": l1_hit_rate,
            "l2_hit_rate": l2_hit_rate,
            "overall_hit_rate": overall_hit_rate,
        }
