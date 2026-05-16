"""
tests/test_health_endpoint.py — Phase 16A
Unit tests untuk /health endpoint (app/api/routes/health.py).
Tidak butuh ES/Redis/LLM — semua komponen di-mock.
Jalankan: pytest tests/test_health_endpoint.py -v -m unit
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.routes.health import health_check


# ─────────────────────────────────────────────────────────────────────────────
# Shared mock factory
# ─────────────────────────────────────────────────────────────────────────────

def _make_mocks(es_up=True, redis_up=True):
    """Buat set mock untuk semua 5 komponen health check."""
    if es_up:
        mock_es = AsyncMock(**{"ping.return_value": True})
    else:
        mock_es = AsyncMock(**{"ping.side_effect": Exception("ES connection refused")})

    mock_main = MagicMock()
    mock_main.es_client = mock_es

    if redis_up:
        mock_redis_client = AsyncMock(**{"ping.return_value": True})
        mock_get_redis = AsyncMock(return_value=mock_redis_client)
    else:
        mock_get_redis = AsyncMock(side_effect=Exception("Redis connection refused"))

    mock_embed_stats = {"size": 42, "hits": 100, "misses": 3}
    mock_tq = MagicMock()
    mock_tq.stats.return_value = {"queue_size": 0, "num_workers": 2, "running": True}
    mock_deepseek = AsyncMock(return_value={"status": "OK", "provider": "deepseek", "reachable": True})

    return mock_main, mock_get_redis, mock_embed_stats, mock_tq, mock_deepseek


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_returns_200_when_all_up():
    """Semua komponen UP → status=UP (HTTP 200, bukan JSONResponse)."""
    mock_main, mock_get_redis, mock_embed_stats, mock_tq, mock_deepseek = _make_mocks()

    with patch.dict("sys.modules", {"app.main": mock_main}), \
         patch("app.utils.cache.get_redis_client", mock_get_redis), \
         patch("app.api.routes.health.get_embedding_cache_stats", return_value=mock_embed_stats), \
         patch("app.api.routes.health.get_task_queue", return_value=mock_tq), \
         patch("app.providers.deepseek.check_deepseek_health", mock_deepseek):
        result = await health_check()

    # Ketika semua UP, returns dict biasa (bukan JSONResponse 503)
    assert isinstance(result, dict)
    assert result["status"] == "UP"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_returns_503_when_es_down():
    """ES ping gagal → status=DOWN, komponen es.status=DOWN."""
    mock_main, mock_get_redis, mock_embed_stats, mock_tq, mock_deepseek = _make_mocks(es_up=False)

    with patch.dict("sys.modules", {"app.main": mock_main}), \
         patch("app.utils.cache.get_redis_client", mock_get_redis), \
         patch("app.api.routes.health.get_embedding_cache_stats", return_value=mock_embed_stats), \
         patch("app.api.routes.health.get_task_queue", return_value=mock_tq), \
         patch("app.providers.deepseek.check_deepseek_health", mock_deepseek):
        result = await health_check()

    from fastapi.responses import JSONResponse
    assert isinstance(result, JSONResponse)
    assert result.status_code == 503

    import json
    body = json.loads(result.body)
    assert body["status"] == "DOWN"
    assert body["components"]["es"]["status"] == "DOWN"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_returns_503_when_redis_down():
    """Redis gagal → status=DOWN, komponen redis.status=DOWN."""
    mock_main, mock_get_redis, mock_embed_stats, mock_tq, mock_deepseek = _make_mocks(redis_up=False)

    with patch.dict("sys.modules", {"app.main": mock_main}), \
         patch("app.utils.cache.get_redis_client", mock_get_redis), \
         patch("app.api.routes.health.get_embedding_cache_stats", return_value=mock_embed_stats), \
         patch("app.api.routes.health.get_task_queue", return_value=mock_tq), \
         patch("app.providers.deepseek.check_deepseek_health", mock_deepseek):
        result = await health_check()

    from fastapi.responses import JSONResponse
    assert isinstance(result, JSONResponse)
    assert result.status_code == 503

    import json
    body = json.loads(result.body)
    assert body["status"] == "DOWN"
    assert body["components"]["redis"]["status"] == "DOWN"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_contains_all_components():
    """Response harus memuat semua 5 key: es, redis, embedding_cache, task_queue, deepseek."""
    mock_main, mock_get_redis, mock_embed_stats, mock_tq, mock_deepseek = _make_mocks()

    with patch.dict("sys.modules", {"app.main": mock_main}), \
         patch("app.utils.cache.get_redis_client", mock_get_redis), \
         patch("app.api.routes.health.get_embedding_cache_stats", return_value=mock_embed_stats), \
         patch("app.api.routes.health.get_task_queue", return_value=mock_tq), \
         patch("app.providers.deepseek.check_deepseek_health", mock_deepseek):
        result = await health_check()

    assert isinstance(result, dict)
    components = result.get("components", {})
    for key in ("es", "redis", "embedding_cache", "task_queue", "deepseek"):
        assert key in components, f"komponen '{key}' tidak ada dalam response"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_embedding_cache_has_stats():
    """embedding_cache dalam response harus memuat key 'size' atau 'hits'."""
    mock_main, mock_get_redis, mock_embed_stats, mock_tq, mock_deepseek = _make_mocks()

    with patch.dict("sys.modules", {"app.main": mock_main}), \
         patch("app.utils.cache.get_redis_client", mock_get_redis), \
         patch("app.api.routes.health.get_embedding_cache_stats", return_value=mock_embed_stats), \
         patch("app.api.routes.health.get_task_queue", return_value=mock_tq), \
         patch("app.providers.deepseek.check_deepseek_health", mock_deepseek):
        result = await health_check()

    assert isinstance(result, dict)
    ec = result["components"]["embedding_cache"]
    assert "size" in ec or "hits" in ec, f"embedding_cache tidak punya stats: {ec}"
