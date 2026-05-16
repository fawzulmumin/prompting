# rag_server.py

import os
import re
import logging

from app.utils.structured_log import configure_logging
configure_logging()

import asyncio
import hashlib
import warnings
from pathlib import Path
from typing import List, Optional, Dict
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks, Depends, Security
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse as _BaseJSONResponse


class JSONResponse(_BaseJSONResponse):
    """Override default response to include charset=utf-8 in Content-Type.
    Prevents emoji/multi-byte Unicode corruption in clients that default to Latin-1."""
    media_type = "application/json; charset=utf-8"
from pydantic import BaseModel, Field, field_validator  # noqa: F401 — kept for back-compat

from app.api.models import AskRequest, AskResponse  # noqa: E402
from app.api.stream import router as stream_router
from app.api.routes.health import router as health_router
from app.services.core.ask_pipeline import run_ask_pipeline
from app.utils.task_queue import get_task_queue

from elasticsearch import AsyncElasticsearch


# ── ROUTING: satu sistem, LLM-based ──────────────────────────────────────────
from app.services.core.cognitive_router import (
    classify_intent,
    QueryRoute,
    is_education_query,
)
from app.services.core.jenjang_constants import JENJANG_MAP
from app.services.core.intent_classifier import (
    classify_intent_rule as rule_classify,
    IntentType as RuleIntentType,
    is_clearly_oot,
)
from app.services.core.multi_intent_orchestrator import orchestrate
from app.services.core.school_name_normalizer import normalize_school_name
from app.services.core.result_model import ResultStatus, build_ambiguous_message
from app.services.location_service import build_reference_locations

# ── HANDLERS ─────────────────────────────────────────────────────────────────
from app.services.query_handlers.list_school_handler import ListSchoolHandler
from app.services.query_handlers.aggregation_handler import (
    handle_aggregation_query,
    detect_ranking_level,
)
from app.services.query_handlers.school_vs_handler import (
    is_school_vs_school_query,
    handle_school_vs_school,
    format_school_comparison_markdown,
    narrate_school_comparison,
)
from app.services.query_handlers.profile_handler import ProfileHandler
from app.services.query_handlers.comparison_handler import handle_comparison_query

# ── NARRATOR ──────────────────────────────────────────────────────────────────
from app.services.narrator_service import (
    narrate_out_of_context,
    narrate_aggregation,
    narrate_comparison_lokasi,
    narrate_comparison_status,
    narrate_school_list,
    narrate_school_profile,
    detect_level,
    narrate_multi_module,
    narrate_qc_percentage,
)

# ── FORMATTER ─────────────────────────────────────────────────────────────────
from app.services.formatters.response_formatter import (
    format_school_answer,
    format_school_list_answer,
    build_reject_answer,
    format_clarification_answer,
    build_location_label,
)

# ── SEARCH & GEO ──────────────────────────────────────────────────────────────
from app.services.search.search_service import (
    perform_search,
    compute_context_sufficiency_score,
    perform_geo_search,
    detect_geo_query,
    geocode_place_name,
    extract_place_from_query,
)

# ── INFRA ─────────────────────────────────────────────────────────────────────
from app.services.location_service import initialize_location_index
from app.services.rate_limit_service import check_rate_limit
from app.services.embedding_service import get_embedding_model
from app.services.confidence_engine import compute_rag_confidence, compute_aggregation_confidence
from app.router import route_with_fallback
from app.providers.deepseek import close_deepseek_client
from app.utils.cache import get_cache, set_cache, get_chat_history, save_chat_history
from app.utils.markdown_to_html import markdown_to_safe_html
from app.utils.rag_evaluation_logger import log_rag_evaluation
from app.background.trigger import trigger_background_jobs

# =================================================
# CONFIG
# =================================================

logger = logging.getLogger(__name__)

warnings.filterwarnings(
    "ignore",
    message="'style' attribute specified, but css_sanitizer not set",
    category=UserWarning,
    module="bleach"
)

es_client = AsyncElasticsearch(
    hosts=[os.getenv("ELASTICSEARCH_NODE")],
    basic_auth=(
        os.getenv("ELASTICSEARCH_USERNAME"),
        os.getenv("ELASTICSEARCH_PASSWORD"),
    ),
    request_timeout=10,
    max_retries=3,
    retry_on_timeout=True
)

profile_handler = ProfileHandler(es_client)

from app.config import ELASTICSEARCH_LIST_INDEX  # default: idx_sekolah_profile
list_handler = ListSchoolHandler(
    es_client,
    ELASTICSEARCH_LIST_INDEX
)
logger.info("ListSchoolHandler index: %s", ELASTICSEARCH_LIST_INDEX)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting application...")
    try:
        await asyncio.to_thread(get_embedding_model)
        await initialize_location_index(es_client)
        await asyncio.to_thread(build_reference_locations)
        await get_task_queue().start()
        from app.utils.arq_pool import get_arq_pool
        await get_arq_pool()
        logger.info("[MAIN] arq pool initialized")
        logger.info("All services initialized")
    except Exception as e:
        logger.error(f"Startup init failed: {e}", exc_info=True)
        raise RuntimeError(f"Startup init failed: {e}") from e
    yield
    logger.info("Shutting down...")
    await asyncio.sleep(1)
    try:
        from app.utils.arq_pool import close_arq_pool
        await close_arq_pool()
    except Exception as e:
        logger.warning(f"Error closing arq pool: {e}")
    try:
        await get_task_queue().stop()
        logger.info("TaskQueue stopped")
    except Exception as e:
        logger.warning(f"Error stopping TaskQueue: {e}")
    try:
        await es_client.close()
        logger.info("Elasticsearch closed")
    except Exception as e:
        logger.warning(f"Error closing ES: {e}")
    try:
        await close_deepseek_client()
        logger.info("DeepSeek client closed")
    except Exception as e:
        logger.warning(f"Error closing DeepSeek client: {e}")


# =================================================
# APP INIT
# =================================================

app = FastAPI(
    title="RAG API Server",
    version="2.0.0",
    lifespan=lifespan,
    default_response_class=JSONResponse,
)

_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(stream_router)
app.include_router(health_router)

# =================================================
# HELPERS
# =================================================

def build_cache_key(query: str, level: Optional[str]) -> str:
    raw = f"{query}:{level}"
    return f"response:{hashlib.md5(raw.encode()).hexdigest()}"


def detect_query_level(query: str) -> Optional[str]:
    q = query.lower()
    patterns = {
        "SD":  r"\b(sd|sekolah dasar)\b",
        "SMP": r"\b(smp|sekolah menengah pertama)\b",
        "SMA": r"\b(sma|smk|sekolah menengah atas)\b",
    }
    for level, p in patterns.items():
        if re.search(p, q):
            return level
    return None


def _extract_radius_from_query(query: str) -> Optional[float]:
    q = query.lower()
    km_patterns = [
        r'radius\s+(\d+(?:\.\d+)?)\s*km',
        r'dalam\s+radius\s+(\d+(?:\.\d+)?)\s*km',
        r'(\d+(?:\.\d+)?)\s*km\b',
        r'(\d+(?:\.\d+)?)\s*kilometer',
    ]
    for pat in km_patterns:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1))
            if 0.1 <= val <= 50:
                return val
    meter_patterns = [
        r'radius\s+(\d+(?:\.\d+)?)\s*(?:m\b|meter)',
        r'dalam\s+(\d+(?:\.\d+)?)\s*(?:m\b|meter)',
        r'(\d+(?:\.\d+)?)\s*meter',
    ]
    for pat in meter_patterns:
        m = re.search(pat, q)
        if m:
            val = float(m.group(1)) / 1000
            if 0.05 <= val <= 5:
                return round(val, 2)
    return None


def _ms(start: datetime) -> int:
    """Hitung durasi dalam ms dari start hingga sekarang."""
    return int((datetime.now() - start).total_seconds() * 1000)


def _base_response(
    query: str,
    start: datetime,
    user_id: str,
    answer_markdown: str,
    provider: str,
    *,
    context_used: List[str] = None,
    context_sufficiency_score: float = 0.0,
    answer_confident: bool = False,
    confidence_score: Optional[float] = None,
    completeness_score: Optional[float] = None,
    num_docs: int = 0,
    from_cache: bool = False,
    suggestions: Optional[List[str]] = None,
) -> AskResponse:
    """
    Helper — bangun AskResponse lengkap dengan semua field wajib.
    Menghindari repetisi 12-field di setiap return statement.
    """
    md = answer_markdown or ""
    return AskResponse(
        query=query,
        answer_markdown=md,
        answer_html=markdown_to_safe_html(md),
        provider=provider,
        from_cache=from_cache,
        duration_ms=_ms(start),
        context_used=context_used or [],
        context_sufficiency_score=context_sufficiency_score,
        answer_confident=answer_confident,
        confidence_score=confidence_score,
        completeness_score=completeness_score,
        user_id=user_id,
        num_docs=num_docs,
        suggestions=suggestions,
    )


def _extract_ambiguous_message(result: dict) -> str | None:
    """Ekstrak pesan AMBIGUOUS dari result handler manapun."""
    if not isinstance(result, dict):
        return None
    status = result.get("status")
    # ResultStatus.AMBIGUOUS bisa berupa enum atau string
    is_ambiguous = (
        status == ResultStatus.AMBIGUOUS
        or status == "AMBIGUOUS"
        or str(status) == "ResultStatus.AMBIGUOUS"
    )
    if not is_ambiguous:
        return None
    data = result.get("data", {})
    # Cek message langsung
    if data.get("message"):
        return data["message"]
    return build_ambiguous_message(data.get("candidates", []))

# =================================================
# ENDPOINT
# =================================================

@app.post("/ask", response_model=AskResponse)
async def ask_question(
    request: AskRequest,
    background_tasks: BackgroundTasks,
    req: Request
):
    start_time = datetime.now()
    query   = normalize_school_name(request.question)
    user_id = request.user_id or (req.client.host if req.client else "anonymous")

    limit_status = await check_rate_limit(user_id)
    if not limit_status or not limit_status.get("allowed"):
        raise HTTPException(429, "Rate limit exceeded")

    result = await run_ask_pipeline(
        query=query, user_id=user_id,
        es_client=es_client, list_handler=list_handler, profile_handler=profile_handler,
        level=request.level or detect_query_level(query),
        lat=request.lat, lon=request.lon,
        radius_km=request.radius_km or 2.0,
        start_time=start_time,
    )

    for func, args, kwargs in result.pop("_bg_tasks", []):
        background_tasks.add_task(func, *args, **kwargs)
    bg_trigger = result.pop("_bg_trigger", None)
    try:
        from app.utils.arq_pool import get_arq_pool
        pool = await get_arq_pool()
        for func, args, kwargs in result.pop("_bg_queue", []):
            job_name = f"job_{func.__name__}"
            try:
                await pool.enqueue_job(job_name, *args, **kwargs)
            except Exception as e:
                logger.warning("[MAIN] arq enqueue failed for %s: %s", job_name, e)
        if bg_trigger:
            try:
                await pool.enqueue_job("job_trigger_background_jobs", payload=bg_trigger)
            except Exception as e:
                logger.warning("[MAIN] arq trigger enqueue failed: %s", e)
    except Exception as e:
        logger.warning("[MAIN] arq pool unavailable, dropping bg_queue: %s", e)
        result.pop("_bg_queue", None)

    clean = {k: v for k, v in result.items() if not k.startswith("_")}
    return AskResponse(**clean)


# =================================================
# ANALYTICS / FEEDBACK LOOP ENDPOINTS
# =================================================

_REPORT_API_KEY = os.getenv("REPORT_API_KEY")
_report_key_header = APIKeyHeader(name="X-Report-Key", auto_error=False)

async def _require_report_key(key: str = Security(_report_key_header)):
    if _REPORT_API_KEY and key != _REPORT_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid report API key")

@app.get("/report/weekly")
async def weekly_report(days: int = 7, top_n: int = 20, _=Depends(_require_report_key)):
    from app.background.analytics import generate_weekly_report
    return generate_weekly_report(days=days, top_n=top_n)


@app.get("/report/ooc-signals")
async def ooc_signals(top_n: int = 50, _=Depends(_require_report_key)):
    from app.background.analytics import extract_ooc_keyword_signals
    tokens = extract_ooc_keyword_signals(top_n=top_n)
    return {
        "count": len(tokens),
        "suggestion": "Pertimbangkan token-token ini untuk ditambahkan ke OOC_KEYWORDS",
        "tokens": tokens,
    }


@app.get("/report/ooc-alerts")
async def ooc_alerts(_=Depends(_require_report_key)):
    import csv
    log_file = Path(__file__).parent / "logs" / "ooc_alert_log.csv"
    if not log_file.exists():
        return {"total": 0, "alerts": []}
    alerts = []
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                alerts.append(row)
    except Exception as e:
        return {"error": str(e)}
    return {"total": len(alerts), "alerts": alerts[-50:]}


# ========== MAIN ==========
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 3000))
    uvicorn.run(
        "rag_server:app",
        host="0.0.0.0",
        port=port,
        reload=(os.getenv("PROD") != "production"),
    )
