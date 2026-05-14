"""
Tests untuk TieredCache (L1 in-memory + L2 Redis).
Jalankan: pytest tests/test_tiered_cache.py -v
Tidak butuh Redis — L2 di-mock.
"""

import sys
import time
import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.tiered_cache import TieredCache


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def make_cache(maxsize: int = 10) -> TieredCache:
    return TieredCache(maxsize=maxsize)


# ─────────────────────────────────────────────────────────────────────────────
# 1. L1 hit — tidak panggil Redis
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_l1_hit_no_redis_call():
    cache = make_cache()
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock) as mock_get, \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock) as mock_set:
        async def _run():
            await cache.set("k1", "val1", ttl_l1=60, ttl_l2=600)
            result = await cache.get("k1")
            return result

        val = run(_run())
        assert val == "val1"
        mock_get.assert_not_called()  # L1 hit, tidak perlu ke Redis


# ─────────────────────────────────────────────────────────────────────────────
# 2. L1 miss, L2 hit — dikembalikan dan L1 dipopulasi
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_l1_miss_l2_hit_populates_l1():
    cache = make_cache()
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock) as mock_get, \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):
        mock_get.return_value = "from_redis"

        async def _run():
            result = await cache.get("k_miss")
            return result

        val = run(_run())
        assert val == "from_redis"
        mock_get.assert_called_once_with("k_miss")
        # Second call should hit L1 (Redis not called again)
        with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock) as mock_get2:
            mock_get2.return_value = None
            val2 = run(cache.get("k_miss"))
            assert val2 == "from_redis"
            mock_get2.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 3. L1 miss, L2 miss — return None
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_l1_miss_l2_miss_returns_none():
    cache = make_cache()
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock) as mock_get, \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):
        mock_get.return_value = None
        val = run(cache.get("no_such_key"))
        assert val is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. TTL L1 expired — cek ulang ke L2
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_l1_ttl_expired_falls_back_to_l2():
    cache = make_cache()
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock) as mock_get, \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):
        mock_get.return_value = "fresh_from_redis"

        async def _run():
            # Set dengan ttl_l1=0 → langsung expired
            await cache._set_l1("kx", "stale", ttl=0)
            # Simulasi waktu berlalu: paksa expire_at ke masa lalu
            async with cache._lock:
                if "kx" in cache._l1:
                    val, _ = cache._l1["kx"]
                    cache._l1["kx"] = (val, time.monotonic() - 1)

            result = await cache.get("kx")
            return result

        val = run(_run())
        assert val == "fresh_from_redis"
        mock_get.assert_called_with("kx")


# ─────────────────────────────────────────────────────────────────────────────
# 5. LRU eviction — entry terlama dievict saat maxsize tercapai
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_lru_eviction_removes_oldest():
    cache = make_cache(maxsize=3)
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock), \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):

        async def _run():
            await cache._set_l1("a", 1, ttl=300)
            await cache._set_l1("b", 2, ttl=300)
            await cache._set_l1("c", 3, ttl=300)
            # "a" adalah oldest
            await cache._set_l1("d", 4, ttl=300)  # evicts "a"
            async with cache._lock:
                return "a" not in cache._l1, "d" in cache._l1

        a_gone, d_present = run(_run())
        assert a_gone, "Entry terlama harus di-evict"
        assert d_present, "Entry terbaru harus ada di L1"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Concurrent access — tidak ada race condition
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_concurrent_access_no_race():
    cache = make_cache(maxsize=50)
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock) as mock_get, \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):
        mock_get.return_value = None

        async def _run():
            tasks = [
                cache.set(f"key{i}", f"val{i}", ttl_l1=60, ttl_l2=600)
                for i in range(20)
            ] + [
                cache.get(f"key{i}")
                for i in range(20)
            ]
            await asyncio.gather(*tasks)
            # After concurrent writes, L1 should have all 20 keys
            return cache.stats()["l1_size"]

        size = run(_run())
        assert size == 20


# ─────────────────────────────────────────────────────────────────────────────
# 7. invalidate — hapus dari L1
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_invalidate_removes_from_l1():
    cache = make_cache()
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock) as mock_get, \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):
        mock_get.return_value = None

        async def _run():
            await cache._set_l1("inv_key", "data", ttl=300)
            await cache.invalidate("inv_key")
            async with cache._lock:
                return "inv_key" not in cache._l1

        gone = run(_run())
        assert gone


# ─────────────────────────────────────────────────────────────────────────────
# 8. stats() — l1_size benar
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_stats_l1_size_accurate():
    cache = make_cache(maxsize=10)
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock), \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):

        async def _run():
            for i in range(5):
                await cache._set_l1(f"s{i}", i, ttl=300)
            return cache.stats()

        s = run(_run())
        assert s["l1_size"] == 5
        assert s["l1_maxsize"] == 10


# ─────────────────────────────────────────────────────────────────────────────
# 9. set ttl_l1=0 → ttl=0 treated as immediate expire
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_set_with_zero_ttl_not_cacheable():
    cache = make_cache()
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock) as mock_get, \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):
        mock_get.return_value = None

        async def _run():
            await cache._set_l1("z", "v", ttl=0)
            # expire_at = 0.0 (already in the past)
            result = await cache.get("z")
            return result

        val = run(_run())
        # After TTL=0, L1 should consider it expired, fall through to L2 (mock returns None)
        assert val is None


# ─────────────────────────────────────────────────────────────────────────────
# 10. Key yang sama di-set dua kali → nilai terbaru dikembalikan
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_overwrite_returns_latest_value():
    cache = make_cache()
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock), \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):

        async def _run():
            await cache.set("dup", "first", ttl_l1=60, ttl_l2=600)
            await cache.set("dup", "second", ttl_l1=60, ttl_l2=600)
            return await cache.get("dup")

        val = run(_run())
        assert val == "second"


# ─────────────────────────────────────────────────────────────────────────────
# 11. Invalidate key yang tidak ada — tidak raise exception
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_invalidate_nonexistent_key_no_error():
    cache = make_cache()

    async def _run():
        await cache.invalidate("this_does_not_exist")  # should not raise

    run(_run())  # no exception


# ─────────────────────────────────────────────────────────────────────────────
# 12. L1 order is maintained correctly (LRU semantics)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_lru_access_updates_order():
    cache = make_cache(maxsize=3)
    with patch("app.utils.tiered_cache.get_cache", new_callable=AsyncMock), \
         patch("app.utils.tiered_cache.set_cache", new_callable=AsyncMock):

        async def _run():
            await cache._set_l1("a", 1, ttl=300)
            await cache._set_l1("b", 2, ttl=300)
            await cache._set_l1("c", 3, ttl=300)
            # Access "a" to make it recently used
            await cache.get("a")
            # Add "d" — should evict "b" (oldest not accessed)
            await cache._set_l1("d", 4, ttl=300)
            async with cache._lock:
                return list(cache._order)

        order = run(_run())
        assert "b" not in order, "b harus dievict (LRU)"
        assert "a" in order, "a tidak boleh dievict (recently accessed)"
