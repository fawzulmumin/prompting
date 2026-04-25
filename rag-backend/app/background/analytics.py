import os
import re
import asyncio
import app.config as config # Ensure environment variables are loaded
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from elasticsearch import AsyncElasticsearch
from elasticsearch.exceptions import ConnectionError, TransportError

logger = logging.getLogger(__name__)

# ================= CONFIG =================
ES_NODE = os.getenv("ELASTICSEARCH_NODE")
ES_USER = os.getenv("ELASTICSEARCH_USERNAME")
ES_PASS = os.getenv("ELASTICSEARCH_PASSWORD")
ES_INDEX = os.getenv("ELASTICSEARCH_ANALYTICS_INDEX")
PROD = os.getenv("PROD", "development")

_es_client: Optional[AsyncElasticsearch] = None
_es_lock = asyncio.Lock()

# ================= CLIENT =================
async def get_es_client() -> Optional[AsyncElasticsearch]:
    global _es_client

    if _es_client:
        return _es_client

    async with _es_lock:
        if _es_client:
            return _es_client
        try:
            _es_client = AsyncElasticsearch(
                hosts=[ES_NODE],
                basic_auth=(ES_USER, ES_PASS),
                request_timeout=10,
                max_retries=2,
                retry_on_timeout=True
            )

            if not await _es_client.ping():
                logger.error("[Analytics] Elasticsearch not responding")
                _es_client = None
                return None

            logger.info("[Analytics] Elasticsearch connected")
            return _es_client

        except Exception as e:
            logger.error(f"[Analytics] ES init failed: {e}")
            _es_client = None
            return None

async def close_analytics_client():
    global _es_client
    if _es_client:
        await _es_client.close()
        _es_client = None


# ================= NLP =================
def extract_category(query: str) -> str:
    q = query.lower()
    patterns = {
        "finance": [r"biaya", r"spp", r"beasiswa"],
        "registration": [r"daftar", r"pendaftaran", r"registrasi"],
        "curriculum": [r"kurikulum", r"mata pelajaran"],
        "facility": [r"fasilitas", r"perpustakaan", r"lab"],
        "policy": [r"aturan", r"kebijakan", r"ppdb"],
        "student": [r"siswa", r"murid", r"pelajar"],
        "teacher": [r"guru", r"pengajar", r"kepala sekolah"],
        "stats": [r"jumlah", r"data", r"statistik"],
    }

    for cat, regs in patterns.items():
        for r in regs:
            if re.search(r, q):
                return cat
    return "general"


def extract_keywords(query: str, limit: int = 5) -> List[str]:
    words = re.findall(r"\b[a-zA-Z]{3,}\b", query.lower())
    stop = {"yang","dan","untuk","dengan","dari","apa","siapa","kapan","dimana","bagaimana"}
    clean = [w for w in words if w not in stop]
    return list(dict.fromkeys(clean))[:limit]


def analyze_sentiment(query: str) -> str:
    q = query.lower()
    if q.endswith("?"):
        return "question"
    if any(w in q for w in ["error","gagal","buruk","jelek"]):
        return "negative"
    if any(w in q for w in ["baik","bagus","terima kasih"]):
        return "positive"
    return "neutral"

# ================= CORE =================
def prepare_analytics(payload: Dict[str, Any]) -> Dict[str, Any]:
    query = payload.get("query", "")
    duration = payload.get("duration_ms", 0)
    context_score = payload.get("context_sufficiency_score", 0.0)
    confident = bool(payload.get("answer_confident", False))
    docs = int(payload.get("num_docs", 0))
    answer = payload.get("answer_markdown", "")

    category = extract_category(query)
    keywords = extract_keywords(query)
    sentiment = analyze_sentiment(query)

    if duration < 1000:
        tier = "fast"
    elif duration < 3000:
        tier = "medium"
    else:
        tier = "slow"

    quality = round((context_score*50) + (20 if confident else 0) + min(docs,5)*4 + min(len(answer)/500,1)*10, 2)

    if not isinstance(quality, (int, float)) or quality != quality: # Cek NaN
        quality = 0.0
        
    return {
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "env": PROD,
        "source": "rag_server",
        "query": query,
        "category": category,
        "keywords": keywords,
        "sentiment": sentiment,
        "provider": payload.get("provider", "deepseek"),
        "duration_ms": duration,
        "performance_tier": tier,
        "from_cache": bool(payload.get("from_cache", False)),
        "context_sufficiency_score": context_score,
        "answer_confident": confident,
        "num_docs": docs,
        "response_length": len(answer),
        "quality_score": quality,
        "is_successful": confident and context_score >= 0.6 and docs > 0,
        "user_id": payload.get("user_id", "anonymous"),
        "has_error": False,
        "error_message": None
    }

# ================= PIPELINE =================
async def run_analytics(payload: Dict[str, Any]) -> bool:
    try:
        client = await get_es_client()
        if not client:
            return False

        doc = prepare_analytics(payload)

        await client.index(index=ES_INDEX, document=doc)

        logger.info(f"[Analytics] indexed | {doc['category']} | {doc['performance_tier']} | Q={doc['quality_score']}")
        return True

    except (ConnectionError, TransportError) as e:
        logger.warning(f"[Analytics] ES down: {e}")
        return False
    except Exception as e:
        if hasattr(e, 'body'):
            logger.error(f"[Analytics] ES Detailed Error: {e.body}") 
        logger.error(f"[Analytics] error: {e}", exc_info=True)
        return False

# ================= BULK =================
async def run_analytics_bulk(payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
    client = await get_es_client()
    if not client:
        return {"total": len(payloads), "success": 0, "failed": len(payloads)}

    ops = []
    for p in payloads:
        doc = prepare_analytics(p)
        ops.append({"index": {"_index": ES_INDEX}})
        ops.append(doc)

    try:
        res = await client.bulk(operations=ops)
    except Exception as e:
        logger.error(f"[Analytics] bulk error: {e}", exc_info=True)
        return {"total": len(payloads), "success": 0, "failed": len(payloads)}

    success = sum(1 for i in res.get("items", []) if i.get("index", {}).get("status") in (200,201))

    return {
        "total": len(payloads),
        "success": success,
        "failed": len(payloads)-success,
        "success_rate": round((success/len(payloads))*100,2) if payloads else 0
    }



# =================================================================
# FEEDBACK LOOP — Item #20
# Tiga mekanisme untuk menutup loop antara low-score events dan
# query improvement:
#   1. OOC Alert    — log khusus jika OOC confidence < 0.3
#   2. Weekly Report — top 20 query dengan skor terendah dari CSV
#   3. OOC Keyword Signal — ekstrak token dari OOC queries → review
# =================================================================

import csv as _csv
from pathlib import Path as _Path

_BASE_DIR = _Path(__file__).parent.parent.parent
_EVAL_LOG  = _BASE_DIR / "logs" / "rag_eval_log.csv"
_OOC_ALERT_LOG = _BASE_DIR / "logs" / "ooc_alert_log.csv"

def _init_ooc_alert_log():
    """Pastikan file OOC alert log ada dengan header."""
    _OOC_ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not _OOC_ALERT_LOG.exists():
        with open(_OOC_ALERT_LOG, "w", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            writer.writerow(["timestamp", "query", "provider", "confidence", "note"])

_init_ooc_alert_log()


def check_ooc_alert(payload: dict) -> bool:
    """
    Alert #1: Jika provider=SYSTEM_OUT_OF_CONTEXT tapi confidence < 0.3,
    log ke ooc_alert_log.csv untuk review developer.
    Return True jika alert ditrigger.
    """
    provider   = payload.get("provider", "")
    confidence = float(payload.get("context_sufficiency_score", 1.0))

    is_ooc = "OUT_OF_CONTEXT" in provider.upper()
    if not (is_ooc and confidence < 0.3):
        return False

    try:
        with open(_OOC_ALERT_LOG, "a", newline="", encoding="utf-8") as f:
            writer = _csv.writer(f)
            writer.writerow([
                datetime.utcnow().isoformat(),
                payload.get("query", ""),
                provider,
                f"{confidence:.4f}",
                "OOC lolos dengan confidence rendah — perlu review OOC_KEYWORDS"
            ])
        logger.warning(
            "[FEEDBACK] OOC Alert: query=%r provider=%s confidence=%.2f",
            payload.get("query"), provider, confidence
        )
        return True
    except Exception as e:
        logger.error("[FEEDBACK] OOC alert write failed: %s", e)
        return False


def generate_weekly_report(days: int = 7, top_n: int = 20) -> dict:
    """
    Report #2: Top N query dengan quality_score terendah dari rag_eval_log.csv.
    Gunakan untuk manual review dan improve intent classifier / OOC_KEYWORDS.

    Returns dict dengan keys:
        - period_days, total_entries, low_score_queries (list), avg_quality
    """
    if not _EVAL_LOG.exists():
        return {"error": "rag_eval_log.csv tidak ditemukan", "total_entries": 0}

    from datetime import timedelta
    cutoff = datetime.now() - timedelta(days=days)

    entries = []
    try:
        with open(_EVAL_LOG, "r", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                try:
                    # Parse timestamp — toleran terhadap berbagai format
                    ts_str = row.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_str)
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue

                    score = float(row.get("context_sufficiency_score") or
                                  row.get("quality_score") or 0)
                    entries.append({
                        "timestamp": ts_str,
                        "query":    row.get("query", ""),
                        "provider": row.get("provider", ""),
                        "score":    score,
                        "confident": row.get("answer_confident", ""),
                    })
                except Exception:
                    continue
    except Exception as e:
        return {"error": str(e), "total_entries": 0}

    if not entries:
        return {"period_days": days, "total_entries": 0, "low_score_queries": []}

    entries.sort(key=lambda x: x["score"])
    avg_quality = sum(e["score"] for e in entries) / len(entries)

    return {
        "period_days":      days,
        "total_entries":    len(entries),
        "avg_quality":      round(avg_quality, 4),
        "low_score_queries": entries[:top_n],
        "generated_at":     datetime.utcnow().isoformat(),
    }


def extract_ooc_keyword_signals(top_n: int = 50) -> list:
    """
    Signal #3: Ekstrak token unik dari query OOC yang sering muncul
    di rag_eval_log.csv (provider = *OUT_OF_CONTEXT* atau confidence < 0.2).
    Output: sorted list token kandidat untuk diperkuat di OOC_KEYWORDS.
    """
    if not _EVAL_LOG.exists():
        return []

    from collections import Counter
    token_counter = Counter()
    STOP = {
        "yang","dan","untuk","dengan","dari","apa","siapa","kapan",
        "dimana","bagaimana","berapa","adalah","di","ke","pada","ini",
        "itu","ada","tidak","bisa","saya","kamu","sekolah","a","an","the"
    }

    try:
        with open(_EVAL_LOG, "r", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                provider = row.get("provider", "")
                try:
                    score = float(row.get("context_sufficiency_score") or 0)
                except ValueError:
                    score = 0.0

                is_ooc = "OUT_OF_CONTEXT" in provider.upper()
                is_low = score < 0.2

                if is_ooc or is_low:
                    query  = row.get("query", "").lower()
                    tokens = re.findall(r"[a-zA-ZÀ-ɏḀ-ỿ]{3,}", query)
                    for t in tokens:
                        if t not in STOP:
                            token_counter[t] += 1
    except Exception as e:
        logger.error("[FEEDBACK] extract_ooc_signals error: %s", e)
        return []

    return [
        {"token": tok, "count": cnt}
        for tok, cnt in token_counter.most_common(top_n)
    ]


# ================= TEST =================
if __name__ == "__main__":
    import asyncio

    async def test():
        payload = {
            "query": "Bagaimana cara mendaftar sekolah?",
            "provider": "deepseek",
            "duration_ms": 1200,
            "context_sufficiency_score": 0.85,
            "answer_confident": True,
            "num_docs": 4,
            "answer_markdown": "Untuk mendaftar sekolah...",
            "user_id": "test_user"
        }

        ok = await run_analytics(payload)
        print("Analytics result:", ok)

    asyncio.run(test())