"""
Tests untuk embedding cache L3 (TieredCache integration) — Phase 15.
Tidak butuh model embedding atau Redis — semua di-mock.
Jalankan: pytest tests/test_embedding_cache.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import hashlib
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_embedding_cache():
    """Reset singleton cache state antara test."""
    from app.services.embedding_service import _embedding_cache
    loop = asyncio.new_event_loop()
    keys = list(_embedding_cache._l1.keys())
    for k in keys:
        loop.run_until_complete(_embedding_cache.invalidate(k))
    loop.close()
    yield


# ── Cache hit/miss behavior ───────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.embedding_service.get_embedding", new_callable=AsyncMock)
async def test_first_call_generates_embedding(mock_get_embed):
    """First call harus generate embedding (cache miss)."""
    mock_get_embed.return_value = [0.1, 0.2, 0.3]

    from app.services.embedding_service import get_cached_embedding

    with patch("app.services.embedding_service._embedding_cache") as mock_cache:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()

        result = await get_cached_embedding("test query")

    mock_get_embed.assert_called_once_with("test query")
    assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
@patch("app.services.embedding_service.get_embedding", new_callable=AsyncMock)
async def test_second_call_hits_cache(mock_get_embed):
    """Second call dengan query sama TIDAK boleh generate embedding lagi."""
    mock_get_embed.return_value = [0.1, 0.2, 0.3]

    from app.services.embedding_service import get_cached_embedding

    with patch("app.services.embedding_service._embedding_cache") as mock_cache:
        mock_cache.get = AsyncMock(side_effect=[None, [0.1, 0.2, 0.3]])
        mock_cache.set = AsyncMock()

        await get_cached_embedding("repeated query")
        result2 = await get_cached_embedding("repeated query")

    # get_embedding hanya dipanggil sekali (cache hit di call ke-2)
    assert mock_get_embed.call_count == 1
    assert result2 == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_empty_text_returns_empty_list():
    """Empty text harus return [] tanpa generate atau cache."""
    from app.services.embedding_service import get_cached_embedding
    result = await get_cached_embedding("")
    assert result == []

    result2 = await get_cached_embedding("   ")
    assert result2 == []


@pytest.mark.asyncio
@patch("app.services.embedding_service.get_embedding", new_callable=AsyncMock)
async def test_failed_embedding_not_cached(mock_get_embed):
    """Jika get_embedding return [] (gagal), jangan simpan ke cache."""
    mock_get_embed.return_value = []

    from app.services.embedding_service import get_cached_embedding

    with patch("app.services.embedding_service._embedding_cache") as mock_cache:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()

        result = await get_cached_embedding("failing query")

    mock_cache.set.assert_not_called()
    assert result == []


@pytest.mark.asyncio
@patch("app.services.embedding_service.get_embedding", new_callable=AsyncMock)
async def test_cache_set_called_with_correct_ttls(mock_get_embed):
    """Pastikan ttl_l1=300 dan ttl_l2=86400 dipakai."""
    mock_get_embed.return_value = [0.5, 0.6]

    from app.services.embedding_service import get_cached_embedding

    with patch("app.services.embedding_service._embedding_cache") as mock_cache:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()

        await get_cached_embedding("ttl test")

    mock_cache.set.assert_called_once()
    call_kwargs = mock_cache.set.call_args
    assert call_kwargs.kwargs.get("ttl_l1") == 300
    assert call_kwargs.kwargs.get("ttl_l2") == 86400


@pytest.mark.asyncio
@patch("app.services.embedding_service.get_embedding", new_callable=AsyncMock)
async def test_cache_key_is_md5_prefixed(mock_get_embed):
    """Cache key harus format 'embed:{md5}'."""
    mock_get_embed.return_value = [0.1]

    from app.services.embedding_service import get_cached_embedding

    with patch("app.services.embedding_service._embedding_cache") as mock_cache:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()

        await get_cached_embedding("key format test")

    get_key = mock_cache.get.call_args[0][0]
    expected_md5 = hashlib.md5("key format test".encode()).hexdigest()
    assert get_key == f"embed:{expected_md5}"


def test_different_texts_different_cache_keys():
    """Dua query berbeda harus punya cache key berbeda."""
    key1 = f"embed:{hashlib.md5('query A'.encode()).hexdigest()}"
    key2 = f"embed:{hashlib.md5('query B'.encode()).hexdigest()}"
    assert key1 != key2


# ── TieredCache singleton integrity ──────────────────────────────────────────

def test_embedding_cache_is_tiered_cache_instance():
    """_embedding_cache harus instance TieredCache."""
    from app.services.embedding_service import _embedding_cache
    from app.utils.tiered_cache import TieredCache
    assert isinstance(_embedding_cache, TieredCache)


def test_embedding_cache_maxsize_512():
    """TieredCache untuk embedding harus maxsize=512."""
    from app.services.embedding_service import _embedding_cache
    assert _embedding_cache._maxsize == 512


def test_embedding_cache_stats_returns_dict():
    """get_embedding_cache_stats() harus return dict dengan l1_size dan l1_maxsize."""
    from app.services.embedding_service import get_embedding_cache_stats
    stats = get_embedding_cache_stats()
    assert isinstance(stats, dict)
    assert "l1_size" in stats
    assert "l1_maxsize" in stats
    assert stats["l1_maxsize"] == 512


# ── Cache error resilience ────────────────────────────────────────────────────

@pytest.mark.asyncio
@patch("app.services.embedding_service.get_embedding", new_callable=AsyncMock)
async def test_cache_get_exception_fallback_to_generate(mock_get_embed):
    """Jika cache.get() raise exception, harus fallback ke generate (tidak crash)."""
    mock_get_embed.return_value = [0.9, 0.8]

    from app.services.embedding_service import get_cached_embedding

    with patch("app.services.embedding_service._embedding_cache") as mock_cache:
        mock_cache.get = AsyncMock(side_effect=Exception("Redis down"))
        mock_cache.set = AsyncMock()

        result = await get_cached_embedding("resilience test")

    assert result == [0.9, 0.8]
    mock_get_embed.assert_called_once()


@pytest.mark.asyncio
@patch("app.services.embedding_service.get_embedding", new_callable=AsyncMock)
async def test_cache_set_exception_does_not_crash(mock_get_embed):
    """Jika cache.set() raise exception, hasil embedding tetap di-return."""
    mock_get_embed.return_value = [0.7]

    from app.services.embedding_service import get_cached_embedding

    with patch("app.services.embedding_service._embedding_cache") as mock_cache:
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock(side_effect=Exception("Redis down"))

        result = await get_cached_embedding("set exception test")

    assert result == [0.7]
