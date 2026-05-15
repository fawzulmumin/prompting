"""
Tests untuk GET /health endpoint — field embedding_cache.
Jalankan: pytest tests/test_health_endpoint.py -v -m unit
Tidak butuh ES/Redis/LLM — panggil health_check() langsung dengan mock.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.main as _main_mod


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_CACHE_STATS = {"l1_size": 10, "l1_maxsize": 512}

_MOCK_ES_UP   = MagicMock()
_MOCK_ES_UP.ping = AsyncMock(return_value=True)

_MOCK_ES_DOWN = MagicMock()
from elasticsearch import ConnectionError as _ESConnErr
_MOCK_ES_DOWN.ping = AsyncMock(side_effect=_ESConnErr("down"))


# ─────────────────────────────────────────────────────────────────────────────
# Tests — ES up, embedding enabled
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthEmbeddingEnabled:
    @pytest.mark.asyncio
    async def test_status_up(self):
        with patch.object(_main_mod, "es_client", _MOCK_ES_UP), \
             patch.object(_main_mod, "EMBEDDING_ENABLED", True), \
             patch.object(_main_mod, "get_embedding_cache_stats", return_value=_CACHE_STATS):
            result = await _main_mod.health_check()
        assert result["status"] == "UP"

    @pytest.mark.asyncio
    async def test_embedding_cache_field_present(self):
        """Field embedding_cache harus ada di response."""
        with patch.object(_main_mod, "es_client", _MOCK_ES_UP), \
             patch.object(_main_mod, "EMBEDDING_ENABLED", True), \
             patch.object(_main_mod, "get_embedding_cache_stats", return_value=_CACHE_STATS):
            result = await _main_mod.health_check()
        assert "embedding_cache" in result

    @pytest.mark.asyncio
    async def test_embedding_cache_has_l1_size(self):
        with patch.object(_main_mod, "es_client", _MOCK_ES_UP), \
             patch.object(_main_mod, "EMBEDDING_ENABLED", True), \
             patch.object(_main_mod, "get_embedding_cache_stats", return_value=_CACHE_STATS):
            result = await _main_mod.health_check()
        ec = result["embedding_cache"]
        assert "l1_size" in ec
        assert "l1_maxsize" in ec

    @pytest.mark.asyncio
    async def test_embedding_cache_values_match_stats(self):
        with patch.object(_main_mod, "es_client", _MOCK_ES_UP), \
             patch.object(_main_mod, "EMBEDDING_ENABLED", True), \
             patch.object(_main_mod, "get_embedding_cache_stats", return_value=_CACHE_STATS):
            result = await _main_mod.health_check()
        ec = result["embedding_cache"]
        assert ec["l1_size"] == 10
        assert ec["l1_maxsize"] == 512


# ─────────────────────────────────────────────────────────────────────────────
# Tests — embedding disabled
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthEmbeddingDisabled:
    @pytest.mark.asyncio
    async def test_embedding_disabled_returns_status_key(self):
        """Jika EMBEDDING_ENABLED=False → embedding_cache: {status: disabled}."""
        with patch.object(_main_mod, "es_client", _MOCK_ES_UP), \
             patch.object(_main_mod, "EMBEDDING_ENABLED", False), \
             patch.object(_main_mod, "get_embedding_cache_stats", return_value=_CACHE_STATS):
            result = await _main_mod.health_check()
        assert result["embedding_cache"] == {"status": "disabled"}

    @pytest.mark.asyncio
    async def test_embedding_disabled_does_not_call_stats(self):
        """get_embedding_cache_stats() tidak dipanggil saat embedding disabled."""
        mock_stats = MagicMock(return_value=_CACHE_STATS)
        with patch.object(_main_mod, "es_client", _MOCK_ES_UP), \
             patch.object(_main_mod, "EMBEDDING_ENABLED", False), \
             patch.object(_main_mod, "get_embedding_cache_stats", mock_stats):
            await _main_mod.health_check()
        mock_stats.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Tests — ES down
# ─────────────────────────────────────────────────────────────────────────────

class TestHealthEsDown:
    @pytest.mark.asyncio
    async def test_status_down(self):
        with patch.object(_main_mod, "es_client", _MOCK_ES_DOWN), \
             patch.object(_main_mod, "EMBEDDING_ENABLED", True), \
             patch.object(_main_mod, "get_embedding_cache_stats", return_value=_CACHE_STATS):
            from fastapi.responses import JSONResponse
            result = await _main_mod.health_check()
        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    @pytest.mark.asyncio
    async def test_embedding_cache_present_in_503_body(self):
        """embedding_cache tetap muncul di body saat ES down."""
        import json
        with patch.object(_main_mod, "es_client", _MOCK_ES_DOWN), \
             patch.object(_main_mod, "EMBEDDING_ENABLED", True), \
             patch.object(_main_mod, "get_embedding_cache_stats", return_value=_CACHE_STATS):
            from fastapi.responses import JSONResponse
            result = await _main_mod.health_check()
        body = json.loads(result.body)
        assert "embedding_cache" in body
