"""
Structured logging untuk RAG Kemendikdasmen.

Emit single-line JSON ke stdout agar mudah di-parse oleh log aggregator
(Datadog, CloudWatch, ELK, dll).

Usage:
    from app.utils.structured_log import get_logger
    logger = get_logger(__name__)

    logger.info("[MODULE] event", extra={"query": "...", "duration_ms": 42})

Panggil configure_logging() SEKALI di startup (app/main.py) sebelum server listen.
Di test, set LOG_FORMAT=plain di environment untuk output yang readable.
"""

import json
import logging
import os
import time
from typing import Any


LOG_FORMAT = os.getenv("LOG_FORMAT", "json")   # "json" | "plain"
LOG_LEVEL  = os.getenv("LOG_LEVEL", "INFO").upper()

_STDLIB_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno",
    "pathname", "filename", "module", "exc_info", "exc_text",
    "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName",
    "process", "message", "taskName",
})


class JsonFormatter(logging.Formatter):
    """Format log record sebagai single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts":     round(time.time(), 3),
            "level":  record.levelname,
            "module": record.name,
            "event":  record.getMessage(),
        }
        for key, val in record.__dict__.items():
            if key not in _STDLIB_ATTRS:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """
    Configure root logger. Idempotent — safe to call more than once.
    Call once at startup in main.py before the server starts listening.
    """
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    root.handlers.clear()

    handler = logging.StreamHandler()
    if LOG_FORMAT == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
        ))
    root.addHandler(handler)

    for noisy in ("elasticsearch", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Shortcut: from app.utils.structured_log import get_logger."""
    return logging.getLogger(name)
