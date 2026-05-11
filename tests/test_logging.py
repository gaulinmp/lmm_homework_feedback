"""Phase 8 — JSON logging + sidecar llm_messages appender."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from app import config as config_module
from app.logging_config import (
    JsonFormatter,
    append_llm_sidecar,
    configure_logging,
)


def test_json_formatter_emits_one_line_json():
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.attempt_id = 42
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["msg"] == "hello world"
    assert payload["attempt_id"] == 42
    assert "ts" in payload


def test_configure_logging_idempotent(monkeypatch):
    """Calling configure_logging twice should not stack handlers."""
    import app.logging_config as lc

    monkeypatch.setattr(lc, "_configured", False)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    monkeypatch.setattr(config_module.settings, "LOG_JSON", True, raising=False)
    monkeypatch.setattr(config_module.settings, "LOG_LEVEL", "DEBUG", raising=False)

    configure_logging()
    n1 = len(root.handlers)
    configure_logging()
    n2 = len(root.handlers)
    assert n1 == n2 == 1
    # Reset for other tests
    monkeypatch.setattr(lc, "_configured", False)


def test_sidecar_writes_jsonl(tmp_path, monkeypatch):
    log_path = tmp_path / "logs" / "llm_messages.jsonl"
    monkeypatch.setattr(
        config_module.settings, "LLM_LOG_PATH", log_path, raising=False
    )
    append_llm_sidecar(
        {
            "ts": "2026-05-11T00:00:00+00:00",
            "role_bucket": "grader",
            "provider": "openai_compat",
            "model": "qwen3",
            "latency_ms": 42,
        }
    )
    append_llm_sidecar(
        {
            "ts": "2026-05-11T00:00:01+00:00",
            "role_bucket": "tutor",
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "latency_ms": 90,
        }
    )

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0]["role_bucket"] == "grader"
    assert rows[1]["role_bucket"] == "tutor"
    assert rows[0]["latency_ms"] == 42


def test_sidecar_creates_parent_dir(tmp_path, monkeypatch):
    log_path = tmp_path / "deep" / "nested" / "llm.jsonl"
    monkeypatch.setattr(
        config_module.settings, "LLM_LOG_PATH", log_path, raising=False
    )
    append_llm_sidecar({"ts": "t", "role_bucket": "tutor"})
    assert log_path.exists()


def test_sidecar_failure_does_not_propagate(monkeypatch, tmp_path, caplog):
    """If the sidecar log path is unwriteable, the helper logs and returns."""
    monkeypatch.setattr(
        config_module.settings,
        "LLM_LOG_PATH",
        Path("/this/does/not/exist/and/cannot/be/created/llm.jsonl"),
        raising=False,
    )
    import app.logging_config as lc

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(lc.Path, "mkdir", _boom)
    with caplog.at_level(logging.WARNING):
        append_llm_sidecar({"ts": "t"})
    assert any("llm sidecar write failed" in m for m in caplog.messages)


def test_router_writes_sidecar_row(tmp_path, monkeypatch):
    """The router's audit writer should also append to the sidecar JSONL file."""
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.engine import Engine
    from sqlalchemy.pool import StaticPool

    from app import db as db_module
    from app.llm import router as rm
    from langchain_core.messages import AIMessage

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fks(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    with engine.begin() as conn:
        for stmt in db_module.SCHEMA_STATEMENTS:
            conn.exec_driver_sql(stmt)
    monkeypatch.setattr(db_module, "_engine", engine)

    log_path = tmp_path / "llm.jsonl"
    monkeypatch.setattr(
        config_module.settings, "LLM_LOG_PATH", log_path, raising=False
    )

    cfg_path = tmp_path / "llm.toml"
    cfg_path.write_text(
        '[roles.grader]\n'
        'provider = "openai_compat"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'model = "qwen3"\n'
    )
    r = rm.LLMRouter(config_path=cfg_path)

    class FakeProvider:
        name = "openai_compat"
        model = "qwen3"

        def invoke(self, messages, *, response_schema=None, files=None):
            return AIMessage(
                content="ok",
                usage_metadata={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
            )

    r._providers["grader"] = FakeProvider()
    r.invoke("grader", [{"role": "user", "content": "ping"}])

    assert log_path.exists()
    rows = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    assert len(rows) == 1
    row = rows[0]
    assert row["role_bucket"] == "grader"
    assert row["provider"] == "openai_compat"
    assert row["model"] == "qwen3"
    assert row["tokens_in"] == 3
    assert row["tokens_out"] == 1
    assert row["error"] is None
    assert "payload" in row and "request" in row["payload"]
