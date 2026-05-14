"""
Tests untuk SSE streaming endpoint POST /ask/stream.
Jalankan: pytest tests/test_sse_stream.py -v -m unit
Tidak butuh Redis, ES, atau DeepSeek — semua di-mock.
"""

import sys
import json
import pytest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from fastapi import FastAPI


_SENTINEL = object()


@contextmanager
def _replace_sys_module(name: str, mock):
    """Swap one sys.modules entry without clearing the whole dict.

    Unlike patch.dict(sys.modules, ...), this does NOT call sys.modules.clear(),
    so unrelated modules (torch, numpy, etc.) survive the context exit.
    """
    original = sys.modules.get(name, _SENTINEL)
    sys.modules[name] = mock
    try:
        yield
    finally:
        if original is _SENTINEL:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_app() -> FastAPI:
    from app.api.stream import router
    app = FastAPI()
    app.include_router(router)
    return app


def _parse_sse(body: bytes) -> list[dict]:
    """Parse raw SSE bytes → list of {event, data} dicts."""
    events = []
    current: dict = {}
    for line in body.decode("utf-8").splitlines():
        if line.startswith("event:"):
            current["event"] = line[len("event:"):].strip()
        elif line.startswith("data:"):
            raw = line[len("data:"):].strip()
            try:
                current["data"] = json.loads(raw)
            except json.JSONDecodeError:
                current["data"] = raw
        elif line == "" and current:
            events.append(current)
            current = {}
    if current:
        events.append(current)
    return events


_DUMMY_RESULT = {
    "query": "test",
    "answer_markdown": "jawaban",
    "answer_html": "<p>jawaban</p>",
    "provider": "TEST",
    "from_cache": False,
    "duration_ms": 50,
    "context_used": [],
    "context_sufficiency_score": 0.9,
    "answer_confident": True,
    "confidence_score": 0.9,
    "completeness_score": 0.9,
    "user_id": "test",
    "num_docs": 1,
    "suggestions": [],
    "_bg_tasks": [],
    "_bg_queue": [],
    "_bg_trigger": None,
}


async def _pipeline_with_steps(**kw):
    """Pipeline mock that emits status steps before returning."""
    on_step = kw.get("on_step")
    if on_step:
        await on_step("classifying", "Mengklasifikasi pertanyaan...")
        await on_step("searching", "Mencari data...")
    return dict(_DUMMY_RESULT)


async def _pipeline_cached(**kw):
    """Pipeline mock that returns a cached result (from_cache=True)."""
    on_step = kw.get("on_step")
    if on_step:
        await on_step("classifying", "Cache hit detected...")
    result = dict(_DUMMY_RESULT)
    result["from_cache"] = True
    return result


async def _pipeline_error(**_kw):
    raise RuntimeError("Simulated pipeline error")


def _mock_main():
    m = MagicMock()
    m.es_client = AsyncMock()
    m.list_handler = MagicMock()
    m.profile_handler = MagicMock()
    return m


async def _run_stream(
    pipeline_fn, question: str = "test query"
) -> tuple[int, list[dict]]:
    """POST /ask/stream with mocked deps. Return (status_code, parsed_events)."""
    app = _make_app()
    with (
        _replace_sys_module("app.main", _mock_main()),
        patch(
            "app.services.core.ask_pipeline.run_ask_pipeline",
            new=AsyncMock(side_effect=pipeline_fn),
        ),
        patch(
            "app.services.rate_limit_service.check_rate_limit",
            new=AsyncMock(return_value={"allowed": True}),
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            async with client.stream(
                "POST", "/ask/stream", json={"question": question}
            ) as resp:
                code = resp.status_code
                body = await resp.aread()

    return code, _parse_sse(body)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Content-Type: text/event-stream
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_content_type_is_event_stream():
    """POST /ask/stream harus return Content-Type: text/event-stream."""
    app = _make_app()
    with (
        _replace_sys_module("app.main", _mock_main()),
        patch(
            "app.services.core.ask_pipeline.run_ask_pipeline",
            new=AsyncMock(side_effect=_pipeline_with_steps),
        ),
        patch(
            "app.services.rate_limit_service.check_rate_limit",
            new=AsyncMock(return_value={"allowed": True}),
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            async with client.stream(
                "POST", "/ask/stream", json={"question": "test"}
            ) as resp:
                content_type = resp.headers.get("content-type", "")
                await resp.aread()

    assert "text/event-stream" in content_type, (
        f"Expected Content-Type text/event-stream, got {content_type!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. "status" event harus muncul sebelum "result"
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_status_event_appears_before_result():
    """SSE stream harus berisi event 'status' sebelum event 'result'."""
    _, events = await _run_stream(_pipeline_with_steps)
    event_types = [e.get("event") for e in events]

    assert "status" in event_types, f"Expected 'status' event. Got: {event_types}"
    assert "result" in event_types, f"Expected 'result' event. Got: {event_types}"

    status_idx = event_types.index("status")
    result_idx = event_types.index("result")
    assert status_idx < result_idx, (
        f"'status' must come before 'result'. "
        f"Indices: status={status_idx}, result={result_idx}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Stream harus berakhir dengan event "done"
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_ends_with_done_event():
    """SSE stream harus berakhir dengan event 'done'."""
    _, events = await _run_stream(_pipeline_with_steps)

    assert events, "Expected at least one SSE event"
    last_event = events[-1]
    assert last_event.get("event") == "done", (
        f"Last event must be 'done', got: {last_event.get('event')!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cached query — stream tetap emit status events + result dari cache
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_cached_query_still_emits_status_and_result():
    """Query di-cache: stream tetap emit status + result dengan from_cache=True."""
    _, events = await _run_stream(_pipeline_cached)
    event_types = [e.get("event") for e in events]

    assert "status" in event_types, (
        f"Cached query must still emit 'status'. Events: {event_types}"
    )
    assert "result" in event_types, (
        f"Cached query must emit 'result'. Events: {event_types}"
    )

    result_event = next(e for e in events if e.get("event") == "result")
    assert result_event["data"].get("from_cache") is True, (
        "Result event data.from_cache must be True for cached query"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Pipeline error → emit event "error", bukan HTTP 500
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_error_emits_error_event_not_http500():
    """Error di pipeline harus muncul sebagai event 'error', bukan HTTP 500."""
    code, events = await _run_stream(_pipeline_error)
    event_types = [e.get("event") for e in events]

    assert code == 200, (
        f"HTTP status must be 200 (SSE error is in-band), got {code}"
    )
    assert "error" in event_types, (
        f"Expected 'error' SSE event for pipeline error. Events: {event_types}"
    )
    error_event = next(e for e in events if e.get("event") == "error")
    assert error_event.get("data"), "Error event must carry a message payload"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Rate limit exceeded → emit "error" event
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_rate_limit_exceeded_emits_error_event():
    """Rate limit exceeded harus emit event 'error' bukan HTTP 429."""
    app = _make_app()
    with (
        _replace_sys_module("app.main", _mock_main()),
        patch(
            "app.services.rate_limit_service.check_rate_limit",
            new=AsyncMock(return_value={"allowed": False}),
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            async with client.stream(
                "POST", "/ask/stream", json={"question": "test"}
            ) as resp:
                code = resp.status_code
                body = await resp.aread()

    events = _parse_sse(body)
    event_types = [e.get("event") for e in events]

    assert code == 200, f"HTTP status must be 200 for SSE error, got {code}"
    assert "error" in event_types, (
        f"Rate limit must produce 'error' SSE event. Events: {event_types}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. result event payload berisi semua field AskResponse
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.asyncio
async def test_result_event_payload_has_required_fields():
    """Event 'result' harus berisi semua field wajib AskResponse."""
    _, events = await _run_stream(_pipeline_with_steps)

    result_events = [e for e in events if e.get("event") == "result"]
    assert result_events, "Must have at least one 'result' event"

    data = result_events[0]["data"]
    required_fields = {
        "query", "answer_markdown", "answer_html", "provider",
        "from_cache", "duration_ms", "context_used",
        "context_sufficiency_score", "answer_confident", "user_id", "num_docs",
    }
    missing = required_fields - data.keys()
    assert not missing, (
        f"Result event missing fields: {missing}. Got keys: {set(data.keys())}"
    )
