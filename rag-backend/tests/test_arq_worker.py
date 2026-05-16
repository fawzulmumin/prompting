"""
Test arq worker job functions — Phase 16C-1.
Unit tests: mock job execution tanpa Redis/koneksi nyata.
Jalankan: pytest tests/test_arq_worker.py -v -m unit
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, patch

from app.background.arq_worker import (
    job_set_cache,
    job_log_rag_evaluation,
    job_trigger_background_jobs,
)

_ctx = {}  # arq passes ctx dict to every job


@pytest.mark.unit
@pytest.mark.asyncio
class TestJobSetCache:
    async def test_calls_set_cache_with_correct_args(self):
        mock_set = AsyncMock(return_value=True)
        with patch("app.utils.cache.set_cache", mock_set):
            await job_set_cache(_ctx, "key:abc", {"data": 1}, 600)
        mock_set.assert_called_once_with("key:abc", {"data": 1}, 600)

    async def test_set_cache_default_ttl(self):
        mock_set = AsyncMock(return_value=True)
        with patch("app.utils.cache.set_cache", mock_set):
            await job_set_cache(_ctx, "key:xyz", {"v": 2})
        mock_set.assert_called_once_with("key:xyz", {"v": 2}, 3600)


@pytest.mark.unit
@pytest.mark.asyncio
class TestJobLogRagEvaluation:
    async def test_passes_kwargs_to_log_rag_evaluation(self):
        mock_log = AsyncMock(return_value=True)
        payload = {
            "query": "daftar sekolah bandung",
            "duration_ms": 250,
            "context_sufficiency_score": 0.85,
            "answer_confident": True,
            "num_docs": 5,
            "answer_length": 300,
        }
        with patch("app.utils.rag_evaluation_logger.log_rag_evaluation", mock_log):
            await job_log_rag_evaluation(_ctx, **payload)
        mock_log.assert_called_once_with(**payload)

    async def test_handles_optional_kwargs(self):
        mock_log = AsyncMock(return_value=True)
        with patch("app.utils.rag_evaluation_logger.log_rag_evaluation", mock_log):
            await job_log_rag_evaluation(
                _ctx,
                query="test",
                duration_ms=100,
                context_sufficiency_score=0.5,
                answer_confident=False,
                num_docs=0,
                answer_length=0,
                from_cache=True,
                cost_usd=0.001,
            )
        call_kwargs = mock_log.call_args.kwargs
        assert call_kwargs["from_cache"] is True
        assert call_kwargs["cost_usd"] == 0.001


@pytest.mark.unit
@pytest.mark.asyncio
class TestJobTriggerBackgroundJobs:
    async def test_passes_payload_to_trigger(self):
        mock_trigger = AsyncMock()
        payload = {"query": "sekolah terbaik", "provider": "list", "user_id": "u1"}
        with patch("app.background.trigger.trigger_background_jobs", mock_trigger):
            await job_trigger_background_jobs(_ctx, payload)
        mock_trigger.assert_called_once_with(payload)
