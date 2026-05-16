"""
arq worker definitions untuk RAG Kemendikdasmen.
Jalankan: arq app.background.arq_worker.WorkerSettings
"""
import os
import logging
from arq.connections import RedisSettings

logger = logging.getLogger(__name__)


async def job_set_cache(ctx, key: str, value: dict, ttl: int = 3600):
    """Job: simpan response ke Redis cache."""
    from app.utils.cache import set_cache
    await set_cache(key, value, ttl)


async def job_log_rag_evaluation(ctx, **kwargs):
    """Job: log evaluasi RAG ke CSV."""
    from app.utils.rag_evaluation_logger import log_rag_evaluation
    await log_rag_evaluation(**kwargs)


async def job_trigger_background_jobs(ctx, payload: dict):
    """Job: trigger analytics, suggestion, rewrite worker."""
    from app.background.trigger import trigger_background_jobs
    await trigger_background_jobs(payload)


async def startup(ctx):
    logger.info("[ARQ] Worker started")


async def shutdown(ctx):
    logger.info("[ARQ] Worker shutting down")


class WorkerSettings:
    functions = [job_set_cache, job_log_rag_evaluation, job_trigger_background_jobs]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(
        os.getenv("REDIS_URL", "redis://localhost:6379")
    )
    max_jobs = 10
    job_timeout = 30
    retry_jobs = True
    max_tries = 3
