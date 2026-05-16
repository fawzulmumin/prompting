"""
app/api/routes/health.py
========================
Health check endpoint — monitoring dan k8s readiness/liveness probe.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.services.embedding_service import get_embedding_cache_stats
from app.utils.task_queue import get_task_queue

router = APIRouter()


@router.get("/health")
async def health_check():
    """Health check: 5 komponen — ES, Redis, embedding_cache, task_queue, deepseek."""
    components: dict = {}
    overall = "UP"

    # 1. Elasticsearch
    try:
        import app.main as _main
        await _main.es_client.ping(request_timeout=2)
        components["es"] = {"status": "UP"}
    except Exception as e:
        components["es"] = {"status": "DOWN", "error": str(e)[:80]}
        overall = "DOWN"

    # 2. Redis
    try:
        from app.utils.cache import get_redis_client
        rc = await get_redis_client()
        await rc.ping()
        components["redis"] = {"status": "UP"}
    except Exception as e:
        components["redis"] = {"status": "DOWN", "error": str(e)[:80]}
        overall = "DOWN"

    # 3. Embedding cache (L1 stats — no network call)
    try:
        stats = get_embedding_cache_stats()
        components["embedding_cache"] = {**stats, "status": "UP"}
    except Exception as e:
        components["embedding_cache"] = {"status": "ERROR", "error": str(e)[:80]}

    # 4. Task queue
    try:
        components["task_queue"] = {**get_task_queue().stats(), "status": "UP"}
    except Exception as e:
        components["task_queue"] = {"status": "ERROR", "error": str(e)[:80]}

    # 5. DeepSeek circuit breaker
    try:
        from app.providers.deepseek import check_deepseek_health
        ds = await check_deepseek_health()
        components["deepseek"] = ds
    except Exception as e:
        components["deepseek"] = {"status": "UNKNOWN", "error": str(e)[:80]}

    payload = {"status": overall, "components": components}
    if overall == "DOWN":
        return JSONResponse(status_code=503, content=payload)
    return payload
