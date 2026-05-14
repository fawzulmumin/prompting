"""
SSE streaming endpoint POST /ask/stream.

Format events:
  event: status  data: {"step": "classifying", "message": "..."}
  event: result  data: {<semua field AskResponse>}
  event: done    data: {}
  event: error   data: {"message": "..."}
"""

import json
import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.models import AskRequest
from app.utils.markdown_to_html import markdown_to_safe_html

logger = logging.getLogger(__name__)

router = APIRouter()


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def ask_stream_generator(
    request_data: dict,
    req: Request,
) -> AsyncGenerator[str, None]:
    from app.services.core.school_name_normalizer import normalize_school_name
    from app.services.core.ask_pipeline import run_ask_pipeline
    from app.services.rate_limit_service import check_rate_limit

    query     = normalize_school_name(request_data.get("question", ""))
    user_id   = request_data.get("user_id") or (req.client.host if req.client else "anonymous")
    level     = request_data.get("level")
    lat       = request_data.get("lat")
    lon       = request_data.get("lon")
    radius_km = request_data.get("radius_km") or 2.0
    start_time = datetime.now()

    # Rate limit check
    try:
        limit_status = await check_rate_limit(user_id)
        if not limit_status or not limit_status.get("allowed"):
            yield _sse("error", {"message": "Rate limit exceeded"})
            return
    except Exception as e:
        logger.warning("[Stream] Rate limit check failed: %s", e)

    # Queue para comunicação produtor → gerador
    yield_queue: asyncio.Queue = asyncio.Queue(maxsize=20)

    async def on_step(step: str, message: str) -> None:
        try:
            yield_queue.put_nowait({"step": step, "message": message})
        except asyncio.QueueFull:
            pass

    async def run_pipeline() -> None:
        try:
            import app.main as _main
            _es   = _main.es_client
            _list = _main.list_handler
            _prof = _main.profile_handler
        except Exception as e:
            logger.error("[Stream] Cannot import app.main singletons: %s", e)
            yield_queue.put_nowait({"_error": f"Internal error: {e}"})
            return

        try:
            result = await run_ask_pipeline(
                query=query, user_id=user_id,
                es_client=_es, list_handler=_list, profile_handler=_prof,
                level=level, lat=lat, lon=lon, radius_km=radius_km,
                start_time=start_time, on_step=on_step,
            )
        except Exception as e:
            logger.exception("[Stream] Pipeline error")
            yield_queue.put_nowait({"_error": f"Pipeline error: {e}"})
            return

        # Non-critical background tasks — fire-and-forget via asyncio
        for func, args, kwargs in result.pop("_bg_queue", []):
            try:
                asyncio.create_task(func(*args, **kwargs))
            except Exception:
                pass
        for func, args, kwargs in result.pop("_bg_tasks", []):
            try:
                asyncio.create_task(func(*args, **kwargs))
            except Exception:
                pass
        bg_trigger = result.pop("_bg_trigger", None)
        if bg_trigger:
            try:
                from app.background.trigger import trigger_background_jobs
                asyncio.create_task(trigger_background_jobs(bg_trigger))
            except Exception:
                pass

        result["answer_html"] = markdown_to_safe_html(result.get("answer_markdown", ""))
        yield_queue.put_nowait({"_result": result})

    pipeline_task = asyncio.create_task(run_pipeline())

    try:
        while True:
            try:
                item = await asyncio.wait_for(yield_queue.get(), timeout=90.0)
            except asyncio.TimeoutError:
                yield _sse("error", {"message": "Pipeline timeout"})
                pipeline_task.cancel()
                return

            if "_error" in item:
                yield _sse("error", {"message": item["_error"]})
                return

            if "_result" in item:
                payload = {k: v for k, v in item["_result"].items() if not k.startswith("_")}
                yield _sse("result", payload)
                yield _sse("done", {})
                return

            yield _sse("status", item)

    except (GeneratorExit, asyncio.CancelledError):
        logger.info("[Stream] Client disconnected | user=%s", user_id)
        pipeline_task.cancel()
    except Exception as e:
        logger.exception("[Stream] Generator error")
        try:
            yield _sse("error", {"message": str(e)})
        except Exception:
            pass
    finally:
        if not pipeline_task.done():
            pipeline_task.cancel()


@router.post("/ask/stream")
async def ask_stream(request: AskRequest, req: Request):
    return StreamingResponse(
        ask_stream_generator(request.model_dump(), req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
