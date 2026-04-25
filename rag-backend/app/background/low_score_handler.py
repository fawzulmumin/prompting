# production-final-clean low_score_handler.py

import asyncio
import csv
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from app.utils.cache import set_cache

logger = logging.getLogger(__name__)

_background_tasks: set = set()

# ================= CONFIG =================
BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "low_score_events.csv"

# ================= INIT =================
def _init_storage():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "query",
                "score",
                "provider",
                "num_docs",
                "duration_ms",
                "user_id",
                "context_used",
                "rewrite_triggered",
                "rewritten_query",
                "rewrite_success",
                "error"
            ])

_init_storage()

# ================= UTIL =================
def _safe(v: Any) -> str:
    if v is None:
        return ""
    s = str(v)
    return s.replace("\n", " ").replace("\r", " ").replace('"', "''")

async def _async_csv_write(row: List[Any]):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_csv_write, row)


def _sync_csv_write(row: List[Any]):
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(row)

# ================= CORE =================
async def handle_low_score_case(
    payload: Dict[str, Any],
    rewrite_threshold: float = 0.45,
    cache_ttl: int = 3600
) -> Dict[str, Any]:
    try:
        query = payload.get("query", "")
        score = float(payload.get("context_sufficiency_score", 0.0))
        provider = payload.get("provider", "deepseek")
        num_docs = int(payload.get("num_docs", 0))
        duration_ms = int(payload.get("duration_ms", 0))
        user_id = payload.get("user_id", "anonymous")
        context_used = payload.get("context_used", "")

        rewrite_triggered = False
        rewritten_query = ""
        rewrite_success = False
        error_msg = ""

        # log base event
        row = [
            datetime.now().isoformat(),
            _safe(query),
            f"{score:.4f}",
            provider,
            num_docs,
            duration_ms,
            user_id,
            _safe(context_used),
            "FALSE",
            "",
            "FALSE",
            ""
        ]

        await _async_csv_write(row)

        # cache low score event
        cache_key = f"low_score:{hashlib.md5(query.encode()).hexdigest()}:{int(datetime.now().timestamp())}"
        await set_cache(cache_key, {
            "query": query,
            "score": score,
            "provider": provider,
            "timestamp": datetime.now().isoformat()
        }, cache_ttl)

        # trigger rewrite
        if score < rewrite_threshold:
            rewrite_triggered = True
            try:
                from app.background.rewrite_worker import rewrite_query
                task = asyncio.create_task(_run_rewrite(query, payload))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            except Exception as e:
                error_msg = str(e)

        return {
            "success": True,
            "rewrite_triggered": rewrite_triggered,
            "score": score,
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.exception("Low score handler failure")
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }


async def _run_rewrite(original_query: str, payload: Dict[str, Any]):
    try:
        from app.background.rewrite_worker import rewrite_query
        res = await rewrite_query(original_query, payload)

        rewritten = res.get("rewritten_query", "") if res else ""
        success = bool(res and res.get("success"))

        # update log
        await _async_csv_write([
            datetime.now().isoformat(),
            _safe(original_query),
            "REWRITE",
            "deepseek",
            "",
            "",
            "",
            "",
            "TRUE",
            _safe(rewritten),
            "TRUE" if success else "FALSE",
            "" if success else res.get("error", "")
        ])

        # cache rewrite
        if success:
            await set_cache(f"rewrite:{hash(original_query)}", {
                "original": original_query,
                "rewritten": rewritten,
                "timestamp": datetime.now().isoformat()
            }, 86400)

    except Exception as e:
        logger.exception("Rewrite worker error")

# ================= ANALYTICS =================
async def get_low_score_summary(days: int = 7) -> Dict[str, Any]:
    if not LOG_FILE.exists():
        return {"total": 0}

    cutoff = datetime.now() - timedelta(days=days)

    total = 0
    total_score = 0.0

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts < cutoff:
                    continue
                score = float(row.get("score", 0) or 0)
                total += 1
                total_score += score
            except Exception:
                continue

    return {
        "days": days,
        "total_events": total,
        "avg_score": round(total_score / total, 4) if total else 0.0
    }

# ================= TEST =================
if __name__ == "__main__":
    async def test():
        res = await handle_low_score_case({
            "query": "info sekolah papua",
            "context_sufficiency_score": 0.2,
            "provider": "deepseek",
            "num_docs": 1,
            "duration_ms": 1200,
            "user_id": "test"
        })
        print(res)
        print(await get_low_score_summary())

    asyncio.run(test())