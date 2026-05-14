# production-final-clean suggestion.py
# Background Job Orchestrator (DeepSeek-only, Async, Production Ready)
import asyncio
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime
from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)

# ==========================================================
# BACKGROUND JOB ORCHESTRATOR
# ==========================================================
async def trigger_background_jobs(
    payload: Dict[str, Any],
    background_tasks: Optional[BackgroundTasks] = None,
) -> Dict[str, Any]:
    """
    background_tasks=None → schedule via asyncio.create_task (SSE path).
    """
    def _schedule(fn, *args):
        if background_tasks is not None:
            background_tasks.add_task(fn, *args)
        else:
            asyncio.create_task(fn(*args))

    try:
        if not payload or not isinstance(payload, dict):
            logger.error("[Trigger] Invalid payload")
            return {"status": "error", "error": "Invalid payload", "jobs_scheduled": 0}

        query = payload.get("query", "unknown")
        logger.info(f"[Trigger] Background jobs for query: {query[:80]}")

        jobs_scheduled = 0
        job_errors = []

        # Analytics Job
        try:
            _schedule(_run_analytics_job, payload)
            jobs_scheduled += 1
        except Exception as e:
            job_errors.append({"job": "analytics", "error": str(e)})

        # Suggestion Job
        try:
            _schedule(_run_suggestion_job, payload)
            jobs_scheduled += 1
        except Exception as e:
            job_errors.append({"job": "suggestion", "error": str(e)})

        # Low Score Job
        try:
            context_score = payload.get("context_sufficiency_score", 0.0)
            if context_score < 0.45:
                _schedule(_run_low_score_job, payload, context_score)
                jobs_scheduled += 1
        except Exception as e:
            job_errors.append({"job": "low_score_handler", "error": str(e)})

        # OOC Alert Job — Item #20 Feedback Loop
        try:
            _schedule(_run_ooc_alert_job, payload)
            jobs_scheduled += 1
        except Exception as e:
            job_errors.append({"job": "ooc_alert", "error": str(e)})

        return {
            "status": "success" if not job_errors else "partial_success",
            "jobs_scheduled": jobs_scheduled,
            "jobs_failed": len(job_errors),
            "errors": job_errors,
            "timestamp": datetime.now().isoformat(),
            "query": query[:120]
        }

    except Exception as e:
        logger.error(f"[Trigger] Fatal error: {str(e)}", exc_info=True)
        return {"status": "error", "error": str(e), "jobs_scheduled": 0}

# ==========================================================
# JOB WRAPPERS
# ==========================================================
async def _run_analytics_job(payload: Dict[str, Any]) -> None:
    from app.background.analytics import run_analytics

    monitor = get_job_monitor()
    start_time = datetime.now()

    try:
        logger.info("[Analytics Job] Start")
        success = await run_analytics(payload)

        await monitor.track_job(
            job_type="analytics",
            start_time=start_time,
            success=bool(success),
            duration=(datetime.now() - start_time).total_seconds(),
            metadata={"query": payload.get("query")}
        )

        logger.info("[Analytics Job] Completed")

    except Exception as e:
        await monitor.track_job(
            job_type="analytics",
            start_time=start_time,
            success=False,
            error=str(e)
        )
        logger.error(f"[Analytics Job] Error: {str(e)}", exc_info=True)


async def _run_suggestion_job(payload: Dict[str, Any]) -> None:
    from app.background.suggestion import generate_suggestions

    monitor = get_job_monitor()
    start_time = datetime.now()

    try:
        logger.info("[Suggestion Job] Start")
        success = await generate_suggestions(payload)

        await monitor.track_job(
            job_type="suggestion",
            start_time=start_time,
            success=bool(success),
            duration=(datetime.now() - start_time).total_seconds(),
            metadata={"query": payload.get("query")}
        )

        logger.info("[Suggestion Job] Completed")

    except Exception as e:
        await monitor.track_job(
            job_type="suggestion",
            start_time=start_time,
            success=False,
            error=str(e)
        )
        logger.error(f"[Suggestion Job] Error: {str(e)}", exc_info=True)


async def _run_low_score_job(payload: Dict[str, Any], score: float) -> None:
    from app.background.low_score_handler import handle_low_score_case

    monitor = get_job_monitor()
    start_time = datetime.now()

    try:
        logger.warning(f"[LowScore Job] Start | score={score}")
        result = await handle_low_score_case(payload)

        await monitor.track_job(
            job_type="low_score_handler",
            start_time=start_time,
            success=result.get("success", False),
            duration=(datetime.now() - start_time).total_seconds(),
            metadata={
                "query": payload.get("query"),
                "score": score,
                "action": result.get("action_taken")
            }
        )

        logger.info("[LowScore Job] Completed")

    except Exception as e:
        await monitor.track_job(
            job_type="low_score_handler",
            start_time=start_time,
            success=False,
            error=str(e)
        )
        logger.error(f"[LowScore Job] Error: {str(e)}", exc_info=True)

# ==========================================================
# BATCH PROCESSING
# ==========================================================
async def trigger_background_jobs_batch(payloads: List[Dict[str, Any]], max_concurrent: int = 10) -> Dict[str, Any]:
    semaphore = asyncio.Semaphore(max_concurrent)

    async def runner(p):
        async with semaphore:
            return await trigger_background_jobs(p)

    tasks = [runner(p) for p in payloads]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    success = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "success")
    partial = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "partial_success")
    failed = len(results) - success - partial

    return {
        "status": "completed",
        "total": len(payloads),
        "success": success,
        "partial_success": partial,
        "failed": failed,
        "success_rate": (success / len(payloads) * 100) if payloads else 0.0,
        "timestamp": datetime.now().isoformat()
    }

# ==========================================================
# JOB MONITORING
# ==========================================================
class JobMonitor:
    def __init__(self):
        self.jobs: Dict[str, List[Dict[str, Any]]] = {
            "analytics": [],
            "suggestion": [],
            "low_score_handler": []
        }
        self.max_history = 1000

    async def track_job(
        self,
        job_type: str,
        start_time: datetime,
        success: bool,
        duration: Optional[float] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        if duration is None:
            duration = (datetime.now() - start_time).total_seconds()

        record = {
            "timestamp": datetime.now().isoformat(),
            "start_time": start_time.isoformat(),
            "duration_s": round(duration, 3),
            "success": success,
            "error": error,
            "metadata": metadata or {}
        }

        if job_type in self.jobs:
            self.jobs[job_type].append(record)
            if len(self.jobs[job_type]) > self.max_history:
                self.jobs[job_type] = self.jobs[job_type][-self.max_history:]

    async def get_stats(self):
        stats = {}
        total_jobs = 0
        total_success = 0
        total_duration = 0.0

        for jt, records in self.jobs.items():
            count = len(records)
            success = sum(1 for r in records if r.get("success"))
            duration = sum(r.get("duration_s", 0) for r in records)

            stats[jt] = {
                "total_jobs": count,
                "success": success,
                "failed": count - success,
                "success_rate": (success / count * 100) if count else 0.0,
                "total_duration": duration
            }

            total_jobs += count
            total_success += success
            total_duration += duration

        stats["_overall"] = {
            "total_jobs": total_jobs,
            "success": total_success,
            "failed": total_jobs - total_success,
            "success_rate": (total_success / total_jobs * 100) if total_jobs else 0.0,
            "total_duration": total_duration
        }

        return stats


_job_monitor: Optional[JobMonitor] = None

def get_job_monitor() -> JobMonitor:
    global _job_monitor
    if _job_monitor is None:
        _job_monitor = JobMonitor()
    return _job_monitor

# ==========================================================
# HEALTH CHECK
# ==========================================================
async def check_background_jobs_health() -> Dict[str, Any]:
    monitor = get_job_monitor()
    stats = await monitor.get_stats()

    failed = stats.get("_overall", {}).get("failed", 0)

    if failed == 0:
        status = "HEALTHY"
    elif failed < 5:
        status = "WARNING"
    else:
        status = "UNHEALTHY"

    return {
        "status": status,
        "failed_jobs": failed,
        "stats": stats,
        "timestamp": datetime.now().isoformat()
    }

async def _run_ooc_alert_job(payload: Dict[str, Any]):
    """Item #20 — Cek dan log OOC query yang lolos dengan confidence rendah."""
    try:
        from app.background.analytics import check_ooc_alert
        check_ooc_alert(payload)
    except Exception as e:
        logger.warning("[Trigger] OOC alert job error: %s", e)
