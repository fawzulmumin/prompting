# Production Final Cost Tracker (DeepSeek Only)
# File: utils/cost_tracker.py

import csv
import logging
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger(__name__)

# ================= CONFIG =================
BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

COST_LOG_FILE = LOG_DIR / "cost_log.csv"
BUDGET_LOG_FILE = LOG_DIR / "budget_log.csv"

# DeepSeek pricing (USD per 1K tokens)
DEEPSEEK_COST = {
    "input_cache_hit": 0.000028,   # $0.028 / 1M
    "input_cache_miss": 0.00028,   # $0.28 / 1M
    "output": 0.00042              # $0.42 / 1M
}


# ================= TOKEN ESTIMATION =================
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    text = str(text).strip()
    if not text:
        return 0
    # heuristic: 1 token ≈ 4 chars
    return (len(text) + 3) // 4

# ================= COST CORE =================
def calculate_cost(input_tokens: int, output_tokens: int) -> Dict[str, Any]:
    input_cost = (input_tokens / 1000) * DEEPSEEK_COST["input_cache_miss"]
    output_cost = (output_tokens / 1000) * DEEPSEEK_COST["output"]
    total_cost = input_cost + output_cost

    def q(x: float) -> float:
        return float(Decimal(str(x)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))

    return {
        "provider": "deepseek",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_cost_usd": q(input_cost),
        "output_cost_usd": q(output_cost),
        "total_cost_usd": q(total_cost),
    }

# ================= CSV WRITER =================
def _write_csv(path: Path, header: List[str], row: List[Any]):
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(header)
        writer.writerow(row)

# ================= LOGGER =================
async def log_cost(
    input_tokens: int,
    output_tokens: int,
    query: str = "",
    duration_ms: int = 0,
    context_docs: int = 0,
) -> float:
    try:
        result = calculate_cost(input_tokens, output_tokens)
        total_cost = result["total_cost_usd"]

        ts = datetime.utcnow().isoformat()
        safe_query = str(query).replace("\n", " ").replace("\r", " ").replace('"', "'")

        row = [
            ts,
            "deepseek",
            input_tokens,
            output_tokens,
            f"{total_cost:.6f}",
            duration_ms,
            context_docs,
            safe_query,
        ]

        header = [
            "timestamp",
            "provider",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "duration_ms",
            "context_docs",
            "query",
        ]

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_csv, COST_LOG_FILE, header, row)

        logger.info(
            f"[COST] deepseek | in={input_tokens} out={output_tokens} | ${total_cost:.6f} | {duration_ms}ms"
        )

        return total_cost
    except Exception as e:
        logger.error(f"Cost log failed: {e}", exc_info=True)
        return 0.0

# ================= SUMMARY =================
async def get_cost_summary(days: int = 30) -> Dict[str, Any]:
    if not COST_LOG_FILE.exists():
        return {
            "total_cost_usd": 0.0,
            "total_requests": 0,
            "avg_cost_per_request": 0.0,
            "period_days": days,
        }

    cutoff = datetime.utcnow() - timedelta(days=days)

    total_cost = 0.0
    total_req = 0
    in_tok = 0
    out_tok = 0

    with open(COST_LOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                if ts < cutoff:
                    continue
                c = float(r["cost_usd"])
                total_cost += c
                total_req += 1
                in_tok += int(r.get("input_tokens", 0))
                out_tok += int(r.get("output_tokens", 0))
            except Exception:
                continue

    avg = total_cost / total_req if total_req else 0.0

    return {
        "provider": "deepseek",
        "total_cost_usd": round(total_cost, 6),
        "total_requests": total_req,
        "avg_cost_per_request": round(avg, 6),
        "total_input_tokens": in_tok,
        "total_output_tokens": out_tok,
        "period_days": days,
        "period_start": cutoff.isoformat(),
        "period_end": datetime.utcnow().isoformat(),
        "currency": "USD",
    }

# ================= DAILY TIMESERIES =================
async def get_daily_costs(days: int = 30) -> List[Dict[str, Any]]:
    if not COST_LOG_FILE.exists():
        return []

    cutoff = datetime.utcnow() - timedelta(days=days)
    bucket: Dict[str, Dict[str, Any]] = {}

    with open(COST_LOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                if ts < cutoff:
                    continue
                d = ts.date().isoformat()
                if d not in bucket:
                    bucket[d] = {
                        "date": d,
                        "cost_usd": 0.0,
                        "requests": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    }
                bucket[d]["cost_usd"] += float(r["cost_usd"])
                bucket[d]["requests"] += 1
                bucket[d]["input_tokens"] += int(r.get("input_tokens", 0))
                bucket[d]["output_tokens"] += int(r.get("output_tokens", 0))
            except Exception:
                continue

    result = sorted(bucket.values(), key=lambda x: x["date"])

    for d in result:
        d["cost_usd"] = round(d["cost_usd"], 6)
        d["avg_cost_per_request"] = round(d["cost_usd"] / d["requests"], 6) if d["requests"] else 0.0

    return result

# ================= BUDGET =================
class BudgetTracker:
    def __init__(self, monthly_budget_usd: float = 100.0):
        self.monthly_budget = monthly_budget_usd

    async def add_expense(self, amount_usd: float, description: str = ""):
        ts = datetime.utcnow().isoformat()
        month = datetime.utcnow().strftime("%Y-%m")

        row = [ts, f"{amount_usd:.6f}", "deepseek", description, month]
        header = ["timestamp", "amount_usd", "provider", "description", "month"]

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _write_csv, BUDGET_LOG_FILE, header, row)

    async def get_monthly_status(self, month: Optional[str] = None) -> Dict[str, Any]:
        if month is None:
            month = datetime.utcnow().strftime("%Y-%m")

        if not BUDGET_LOG_FILE.exists():
            return {
                "month": month,
                "budget_usd": self.monthly_budget,
                "spent_usd": 0.0,
                "remaining_usd": self.monthly_budget,
                "percentage_used": 0.0,
                "status": "OK",
            }

        spent = 0.0
        with open(BUDGET_LOG_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("month") == month:
                    try:
                        spent += float(r["amount_usd"])
                    except Exception:
                        pass

        remaining = self.monthly_budget - spent
        pct = (spent / self.monthly_budget * 100) if self.monthly_budget else 0.0

        if pct >= 100:
            status = "OVER_BUDGET"
        elif pct >= 90:
            status = "CRITICAL"
        elif pct >= 75:
            status = "WARNING"
        elif pct >= 50:
            status = "APPROACHING"
        else:
            status = "OK"

        return {
            "month": month,
            "budget_usd": self.monthly_budget,
            "spent_usd": round(spent, 6),
            "remaining_usd": round(remaining, 6),
            "percentage_used": round(pct, 2),
            "status": status,
        }

# ================= SELF TEST =================
if __name__ == "__main__":
    async def test():
        print("Testing DeepSeek Cost Tracker...")
        cost = await log_cost(1200, 600, "test query", 800, 3)
        print("Logged cost:", cost)

        summary = await get_cost_summary(7)
        print("Summary:", summary)

        daily = await get_daily_costs(7)
        print("Daily:", daily[-3:])

        budget = BudgetTracker(20.0)
        await budget.add_expense(2.5, "api usage")
        status = await budget.get_monthly_status()
        print("Budget:", status)

    asyncio.run(test())
