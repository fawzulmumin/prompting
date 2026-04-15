# ===============================
# ragEvaluationLogger.py
# Production Final Version (DeepSeek Only)
# ===============================

import csv
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import asyncio
import re
from filelock import FileLock

logger = logging.getLogger(__name__)

# ========== CONFIGURATION ==========
LOG_DIR = Path(__file__).parent.parent / "logs"
EVAL_LOG_FILE = LOG_DIR / "rag_eval_log.csv"
LOCK_FILE = LOG_DIR / "rag_eval_log.lock"

PROVIDER_NAME = "deepseek"  # single-provider normalization

# Retention policy
MAX_LOG_DAYS = 90

# ========== INITIALIZATION ==========
def _initialize_log_file() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not EVAL_LOG_FILE.exists():
        with open(EVAL_LOG_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "query",
                "provider",
                "duration_ms",
                "context_score",
                "answer_confident",
                "num_docs",
                "answer_length",
                "answer_preview",
                "context_used",
                "from_cache",
                "cost_usd",
                "input_tokens",
                "output_tokens"
            ])
        logger.info("RAG evaluation log initialized")

_initialize_log_file()

# ========== HELPERS ==========
def _sanitize(text: Any) -> str:
    if text is None:
        return ""
    text = str(text)
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = text.replace('"', '""')
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
    return text.strip()


def _preview(answer: str, max_len: int = 120) -> str:
    if not answer:
        return ""
    answer = answer.strip()
    if len(answer) <= max_len:
        return answer
    cut = answer[:max_len]
    if ' ' in cut:
        cut = cut[:cut.rfind(' ')]
    return cut + "..."

# ========== CORE LOGGER ==========
async def log_rag_evaluation(
    query: str,
    duration_ms: int,
    context_sufficiency_score: float,
    answer_confident: bool,
    num_docs: int,
    answer_length: int,
    answer_markdown: Optional[str] = None,
    context_used: Optional[str] = None,
    from_cache: bool = False,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0
) -> bool:
    try:
        timestamp = datetime.utcnow().isoformat()

        # normalize score to 0–1
        if context_sufficiency_score > 1:
            context_sufficiency_score = context_sufficiency_score / 100

        row = {
            "timestamp": timestamp,
            "query": _sanitize(query),
            "provider": PROVIDER_NAME,
            "duration_ms": int(duration_ms),
            "context_score": round(float(context_sufficiency_score), 4),
            "answer_confident": "TRUE" if answer_confident else "FALSE",
            "num_docs": int(num_docs),
            "answer_length": int(answer_length),
            "answer_preview": _sanitize(_preview(answer_markdown or "")),
            "context_used": _sanitize(context_used),
            "from_cache": "TRUE" if from_cache else "FALSE",
            "cost_usd": round(float(cost_usd), 6),
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens)
        }

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_row, row)
        return True

    except Exception as e:
        logger.error(f"RAG eval logging failed: {e}", exc_info=True)
        return False


def _write_row(row: Dict[str, Any]) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(LOCK_FILE))

    with lock:
        with open(EVAL_LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                row["timestamp"],
                row["query"],
                row["provider"],
                row["duration_ms"],
                row["context_score"],
                row["answer_confident"],
                row["num_docs"],
                row["answer_length"],
                row["answer_preview"],
                row["context_used"],
                row["from_cache"],
                row["cost_usd"],
                row["input_tokens"],
                row["output_tokens"],
            ])

# ========== RETENTION ==========
async def cleanup_old_logs(days: int = MAX_LOG_DAYS) -> Dict[str, Any]:
    if not EVAL_LOG_FILE.exists():
        return {"status": "NO_FILE"}

    cutoff = datetime.utcnow() - timedelta(days=days)
    kept = []

    lock = FileLock(str(LOCK_FILE))
    with lock:
        with open(EVAL_LOG_FILE, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames
            for row in reader:
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                    if ts >= cutoff:
                        kept.append(row)
                except:
                    pass

        with open(EVAL_LOG_FILE, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(kept)

    return {
        "status": "SUCCESS",
        "kept": len(kept),
        "cutoff": cutoff.isoformat()
    }

# ========== ANALYTICS ==========
async def get_summary(days: int = 7) -> Dict[str, Any]:
    if not EVAL_LOG_FILE.exists():
        return {}

    cutoff = datetime.utcnow() - timedelta(days=days)

    total = 0
    total_score = 0
    total_duration = 0
    confident = 0
    cache_hits = 0
    total_cost = 0

    with open(EVAL_LOG_FILE, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts < cutoff:
                    continue

                total += 1
                score = float(row["context_score"])
                total_score += score
                total_duration += int(row["duration_ms"])
                total_cost += float(row["cost_usd"])

                if row["answer_confident"] == "TRUE":
                    confident += 1
                if row["from_cache"] == "TRUE":
                    cache_hits += 1

            except:
                continue

    if total == 0:
        return {}

    return {
        "total_requests": total,
        "avg_context_score": round(total_score / total, 4),
        "avg_duration_ms": int(total_duration / total),
        "confidence_rate": round(confident / total * 100, 2),
        "cache_hit_rate": round(cache_hits / total * 100, 2),
        "total_cost_usd": round(total_cost, 6),
        "provider": PROVIDER_NAME
    }

# ========== HEALTH ==========
def health_check() -> Dict[str, Any]:
    return {
        "log_file": str(EVAL_LOG_FILE),
        "exists": EVAL_LOG_FILE.exists(),
        "size_bytes": EVAL_LOG_FILE.stat().st_size if EVAL_LOG_FILE.exists() else 0,
        "provider": PROVIDER_NAME
    }

# ========== TEST ==========
if __name__ == "__main__":
    async def test():
        await log_rag_evaluation(
            query="Apa itu AI?",
            duration_ms=1200,
            context_sufficiency_score=0.82,
            answer_confident=True,
            num_docs=4,
            answer_length=350,
            answer_markdown="AI adalah kecerdasan buatan...",
            context_used="KB_AI",
            cost_usd=0.00032,
            input_tokens=1200,
            output_tokens=500
        )

        summary = await get_summary(7)
        print("SUMMARY:", summary)
        print("HEALTH:", health_check())

    asyncio.run(test())
