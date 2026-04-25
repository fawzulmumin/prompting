# background/rewrite_worker.py
# Production Final Clean - DeepSeek Only
# Enterprise-grade Query Rewrite Engine

import csv
import hashlib
import re
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta

from app.utils.cache import get_cache, set_cache

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_DIR = Path(__file__).parent.parent
LOG_DIR = BASE_DIR / "logs"
REWRITE_LOG_FILE = LOG_DIR / "rewritten_queries.csv"

LOG_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# LOG INITIALIZATION
# =============================================================================

def _init_log_file() -> None:
    if not REWRITE_LOG_FILE.exists():
        with open(REWRITE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "original_query",
                "rewritten_query",
                "strategies",
                "original_length",
                "rewritten_length",
                "improvement_score",
                "context_score",
                "provider"
            ])
        logger.info(f"[RewriteWorker] Log file created: {REWRITE_LOG_FILE}")

_init_log_file()

# =============================================================================
# UTILITIES
# =============================================================================

def _sanitize(text: str) -> str:
    if not text:
        return ""
    return str(text).replace("\n", " ").replace("\r", " ").replace('"', '""').strip()

def _write_log_row(row: Dict[str, Any]) -> None:
    try:
        with open(REWRITE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                row["timestamp"],
                row["original_query"],
                row["rewritten_query"],
                row["strategies"],
                row["original_length"],
                row["rewritten_length"],
                row["improvement_score"],
                row["context_score"],
                row["provider"]
            ])
    except Exception as e:
        logger.error(f"[RewriteWorker] CSV write error: {e}")

# =============================================================================
# REWRITE STRATEGIES
# =============================================================================

def _length_strategy(q: str) -> Optional[str]:
    if len(q) < 20:
        return f"Jelaskan secara rinci dan terstruktur tentang: {q}"
    return None

def _clarity_strategy(q: str) -> Optional[str]:
    ql = q.lower()
    if not any(k in ql for k in ["bagaimana", "cara", "langkah"]):
        return f"{q} (jelaskan mekanisme, alur, dan langkah-langkahnya)"
    return None

def _keyword_strategy(q: str) -> Optional[str]:
    ql = q.lower()
    modified = q

    if re.search(r"\b(apa itu|pengertian|definisi)\b", ql):
        if "secara rinci" not in ql:
            modified += " secara rinci dan komprehensif"

    if re.search(r"\b(syarat|prosedur|mekanisme)\b", ql):
        if "langkah-langkah" not in ql:
            modified += " dengan langkah-langkah lengkap dan contoh"

    if re.search(r"\b(berapa|jumlah|data|statistik)\b", ql):
        if "sumber" not in ql:
            modified += " berdasarkan data terbaru dan sertakan sumber resmi"

    return modified if modified != q else None

def _context_strategy(q: str, payload: Optional[Dict[str, Any]]) -> Optional[str]:
    if not payload:
        return None

    ctx = str(payload.get("context_used", "")).lower()
    level = None

    if any(k in ctx for k in ["sd", "sekolah dasar"]):
        level = "SD"
    elif any(k in ctx for k in ["smp", "menengah pertama"]):
        level = "SMP"
    elif any(k in ctx for k in ["sma", "smk", "menengah atas"]):
        level = "SMA/SMK"

    if level and level not in q:
        return f"{q} (khusus untuk jenjang {level})"

    return None

# =============================================================================
# CORE ENGINE
# =============================================================================

async def rewrite_query(
    original_query: str,
    payload: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:

    if not original_query or not original_query.strip():
        return {
            "success": False,
            "error": "empty_query",
            "original_query": original_query,
            "rewritten_query": original_query
        }

    base_query = original_query.strip()
    query = base_query
    strategies: List[str] = []

    # Strategy pipeline
    v = _length_strategy(query)
    if v:
        query = v
        strategies.append("length_enhancement")

    v = _clarity_strategy(query)
    if v:
        query = v
        strategies.append("clarity_enhancement")

    v = _keyword_strategy(query)
    if v:
        query = v
        strategies.append("keyword_enhancement")

    v = _context_strategy(query, payload)
    if v:
        query = v
        strategies.append("context_enhancement")

    rewritten = query.strip()

    original_len = len(base_query)
    rewritten_len = len(rewritten)

    # Improvement score (0–100)
    length_gain = max(rewritten_len - original_len, 0)
    length_score = min(length_gain * 2.0, 40.0)
    strategy_score = min(len(strategies) * 15.0, 60.0)
    improvement_score = round(min(length_score + strategy_score, 100.0), 2)

    row = {
        "timestamp": datetime.now().isoformat(),
        "original_query": _sanitize(base_query),
        "rewritten_query": _sanitize(rewritten),
        "strategies": ";".join(strategies) if strategies else "none",
        "original_length": original_len,
        "rewritten_length": rewritten_len,
        "improvement_score": improvement_score,
        "context_score": payload.get("context_sufficiency_score", 0.0) if payload else 0.0,
        "provider": payload.get("provider", "deepseek") if payload else "deepseek"
    }

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _write_log_row, row)

    logger.info(
        f"[RewriteWorker] {strategies if strategies else ['none']} | "
        f"'{base_query[:40]}' -> '{rewritten[:40]}' | score={improvement_score}"
    )

    return {
        "success": True,
        "original_query": base_query,
        "rewritten_query": rewritten,
        "strategies_applied": strategies,
        "improvement_score": improvement_score,
        "original_length": original_len,
        "rewritten_length": rewritten_len
    }

# =============================================================================
# CACHE LAYER
# =============================================================================

def _cache_key(q: str) -> str:
    return f"rewrite:{hashlib.md5(q.lower().strip().encode()).hexdigest()}"

async def get_cached_rewrite(original_query: str) -> Optional[str]:
    try:
        key = _cache_key(original_query)
        data = await get_cache(key)
        if data and data.get("rewritten"):
            return data["rewritten"]
    except Exception:
        pass
    return None

async def cache_rewrite(original_query: str, rewritten_query: str, ttl: int = 604800) -> None:
    key = _cache_key(original_query)
    await set_cache(key, {
        "original": original_query,
        "rewritten": rewritten_query,
        "cached_at": datetime.now().isoformat()
    }, ttl)

async def smart_rewrite_with_cache(
    original_query: str,
    payload: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:

    cached = await get_cached_rewrite(original_query)
    if cached:
        return {
            "success": True,
            "original_query": original_query,
            "rewritten_query": cached,
            "from_cache": True,
            "strategies_applied": ["cache_hit"]
        }

    result = await rewrite_query(original_query, payload)

    if result.get("success"):
        await cache_rewrite(original_query, result["rewritten_query"])
        result["cached"] = True

    result["from_cache"] = False
    return result

# =============================================================================
# ANALYTICS
# =============================================================================

async def get_rewrite_statistics(days: int = 30) -> Dict[str, Any]:
    if not REWRITE_LOG_FILE.exists():
        return {"total": 0}

    cutoff = datetime.now() - timedelta(days=days)
    total = 0
    total_score = 0.0
    strategy_count: Dict[str, int] = {}

    with open(REWRITE_LOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                if ts < cutoff:
                    continue
                total += 1
                total_score += float(row.get("improvement_score", 0))
                for s in row.get("strategies", "").split(";"):
                    if s and s != "none":
                        strategy_count[s] = strategy_count.get(s, 0) + 1
            except Exception:
                continue

    return {
        "period_days": days,
        "total_rewrites": total,
        "avg_improvement": round(total_score / total, 2) if total else 0.0,
        "strategy_usage": dict(sorted(strategy_count.items(), key=lambda x: x[1], reverse=True))
    }

async def get_top_rewrites(limit: int = 10) -> List[Dict[str, Any]]:
    if not REWRITE_LOG_FILE.exists():
        return []

    with open(REWRITE_LOG_FILE, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    rows.sort(key=lambda r: float(r.get("improvement_score", 0)), reverse=True)

    return rows[:limit]

# =============================================================================
# HEALTH CHECK
# =============================================================================

async def health_check() -> Dict[str, Any]:
    return {
        "service": "rewrite_worker",
        "status": "ok",
        "log_file": str(REWRITE_LOG_FILE),
        "log_exists": REWRITE_LOG_FILE.exists(),
        "timestamp": datetime.now().isoformat()
    }

# =============================================================================
# TEST MODE
# =============================================================================

if __name__ == "__main__":
    async def _test():
        q = "kurikulum merdeka"
        res = await smart_rewrite_with_cache(q, {"provider": "deepseek"})
        print(res)

        stats = await get_rewrite_statistics(30)
        print("\nSTATS:", stats)

        top = await get_top_rewrites(5)
        print("\nTOP:", top[:2])

    asyncio.run(_test())
