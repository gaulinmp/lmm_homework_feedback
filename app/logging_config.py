"""Structured logging configuration.

Two outputs:
- Process logs to stderr (picked up by journald in production), optionally
  encoded as one JSON record per line when ``settings.LOG_JSON`` is true.
- A sidecar JSON-lines file at ``settings.LLM_LOG_PATH`` for every
  ``llm_messages`` row written by ``app.llm.router``. The DB row is the
  source of truth; the sidecar exists so offline analytics can grep without
  touching SQLite during a request.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings


_STANDARD_LOGRECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter — one record per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def configure_logging() -> None:
    """Wire up root logging. Idempotent; safe to call from app startup."""
    global _configured
    if _configured:
        return

    level_name = (settings.LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    if settings.LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    root.addHandler(handler)

    for noisy in ("httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


_sidecar_lock = threading.Lock()


def append_llm_sidecar(record: dict[str, Any]) -> None:
    """Append one JSON-lines row to the sidecar log of LLM exchanges.

    Best-effort: any IO error is logged at WARNING and swallowed so a broken
    log volume can't block a grader request.
    """
    path = Path(settings.LLM_LOG_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _sidecar_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        logging.getLogger(__name__).warning(
            "llm sidecar write failed: %r path=%s", e, path
        )


__all__ = ["configure_logging", "append_llm_sidecar", "JsonFormatter"]
