from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage, BaseMessage
from sqlalchemy import text

from app.config import settings
from app.db import get_engine


PROVIDER_CLASSES: dict[str, str] = {
    "openai_compat": "app.llm.providers.openai_compat:OpenAICompatProvider",
    "openai": "app.llm.providers.openai:OpenAIProvider",
    "anthropic": "app.llm.providers.anthropic:AnthropicProvider",
    "gemini": "app.llm.providers.gemini:GeminiProvider",
}


# Per-role default worker counts. Local llama.cpp roles serve one request at a
# time so we throttle to 1. Cloud roles are effectively unbounded.
_LOCAL_BASE_URL_PREFIXES = ("http://127.0.0.1", "http://localhost")
_UNBOUNDED_WORKERS = 1024


class RoleBucket:
    """Per-role concurrency gate with a visible waiting count.

    Wraps an ``asyncio.Semaphore`` and tracks how many coroutines are blocked
    waiting for a slot. The waiting count is exposed for the queue-position UI.
    """

    def __init__(self, workers: int) -> None:
        self.workers = workers
        self._sem = asyncio.Semaphore(workers)
        self.waiting = 0
        self.in_flight = 0

    @contextlib.asynccontextmanager
    async def acquire(self):
        self.waiting += 1
        try:
            await self._sem.acquire()
        except BaseException:
            self.waiting -= 1
            raise
        self.waiting -= 1
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1
            self._sem.release()


def _load_provider_class(name: str):
    if name not in PROVIDER_CLASSES:
        raise ValueError(f"Unknown LLM provider: {name!r}")
    module_path, cls_name = PROVIDER_CLASSES[name].split(":")
    return getattr(importlib.import_module(module_path), cls_name)


def _serialize_messages(messages) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, BaseMessage):
            out.append({"role": m.type, "content": m.content})
        elif isinstance(m, dict):
            out.append({"role": m.get("role"), "content": m.get("content")})
        else:
            out.append({"role": "user", "content": str(m)})
    return out


def _serialize_response(msg: AIMessage | None) -> dict[str, Any] | None:
    if msg is None:
        return None
    parsed = msg.additional_kwargs.get("parsed") if msg.additional_kwargs else None
    if hasattr(parsed, "model_dump"):
        parsed = parsed.model_dump()
    return {
        "role": "assistant",
        "content": msg.content,
        "usage_metadata": getattr(msg, "usage_metadata", None),
        "response_metadata": getattr(msg, "response_metadata", None),
        "parsed": parsed,
    }


class LLMRouter:
    """Loads role -> (provider, model, ...) from config/llm.toml and dispatches.

    Every `invoke` writes one row to `llm_messages`, even on error.
    """

    def __init__(self, config_path: Path | str | None = None) -> None:
        path = Path(config_path) if config_path else Path(settings.LLM_CONFIG_PATH)
        with path.open("rb") as f:
            config = tomllib.load(f)
        self._roles: dict[str, dict[str, Any]] = config.get("roles", {})
        self._providers: dict[str, Any] = {}
        self._role_buckets: dict[str, RoleBucket] = {}
        self._user_locks: dict[int, asyncio.Lock] = {}

    def roles(self) -> list[str]:
        return list(self._roles.keys())

    def _default_workers_for(self, role: str) -> int:
        cfg = self._roles.get(role) or {}
        if "workers" in cfg:
            try:
                return max(1, int(cfg["workers"]))
            except (TypeError, ValueError):
                pass
        base_url = (cfg.get("base_url") or "").lower()
        if any(base_url.startswith(p) for p in _LOCAL_BASE_URL_PREFIXES):
            return 1
        provider = cfg.get("provider")
        if provider == "openai_compat" and not base_url:
            return 1
        return _UNBOUNDED_WORKERS

    def role_bucket(self, role: str) -> RoleBucket:
        """Return (creating if necessary) the per-role concurrency bucket."""
        if role not in self._role_buckets:
            self._role_buckets[role] = RoleBucket(self._default_workers_for(role))
        return self._role_buckets[role]

    def queue_status(self, role: str) -> dict[str, int]:
        """Snapshot of how many requests are waiting / running for a role."""
        bucket = self._role_buckets.get(role)
        if bucket is None:
            return {
                "waiting": 0,
                "in_flight": 0,
                "workers": self._default_workers_for(role),
            }
        return {
            "waiting": bucket.waiting,
            "in_flight": bucket.in_flight,
            "workers": bucket.workers,
        }

    def user_lock(self, user_id: int) -> asyncio.Lock:
        """Per-user asyncio.Lock — created lazily, persists for process lifetime."""
        lock = self._user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_locks[user_id] = lock
        return lock

    def _provider_for(self, role: str):
        if role not in self._roles:
            raise KeyError(f"No LLM role configured: {role!r}")
        if role not in self._providers:
            cfg = dict(self._roles[role])
            provider_name = cfg.pop("provider")
            cls = _load_provider_class(provider_name)
            inst = cls(**cfg)
            inst.name = provider_name
            self._providers[role] = inst
        return self._providers[role]

    def invoke(
        self,
        role: str,
        messages,
        *,
        response_schema=None,
        files=None,
        attempt_id: int | None = None,
        submission_id: int | None = None,
    ) -> AIMessage:
        provider = self._provider_for(role)
        provider_name = provider.name
        model = provider.model
        start = time.perf_counter()
        result: AIMessage | None = None
        error: str | None = None
        try:
            result = provider.invoke(messages, response_schema=response_schema, files=files)
            return result
        except Exception as e:
            error = repr(e)
            raise
        finally:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self._write_audit_row(
                role_bucket=role,
                provider_name=provider_name,
                model=model,
                messages=messages,
                response=result,
                error=error,
                latency_ms=latency_ms,
                attempt_id=attempt_id,
                submission_id=submission_id,
            )

    async def astream(
        self,
        role: str,
        messages,
        *,
        response_schema=None,
        files=None,
        attempt_id: int | None = None,
        submission_id: int | None = None,
    ) -> AsyncIterator[str]:
        provider = self._provider_for(role)
        provider_name = provider.name
        model = provider.model
        start = time.perf_counter()
        chunks: list[str] = []
        error: str | None = None
        try:
            async for piece in provider.astream(
                messages, response_schema=response_schema, files=files
            ):
                chunks.append(piece)
                yield piece
        except Exception as e:
            error = repr(e)
            raise
        finally:
            latency_ms = int((time.perf_counter() - start) * 1000)
            joined = "".join(chunks)
            response = AIMessage(content=joined) if joined or error is None else None
            self._write_audit_row(
                role_bucket=role,
                provider_name=provider_name,
                model=model,
                messages=messages,
                response=response,
                error=error,
                latency_ms=latency_ms,
                attempt_id=attempt_id,
                submission_id=submission_id,
            )

    def _write_audit_row(
        self,
        *,
        role_bucket: str,
        provider_name: str,
        model: str,
        messages,
        response: AIMessage | None,
        error: str | None,
        latency_ms: int,
        attempt_id: int | None,
        submission_id: int | None,
    ) -> None:
        usage = (response.usage_metadata if response is not None else None) or {}
        tokens_in = usage.get("input_tokens") if isinstance(usage, dict) else None
        tokens_out = usage.get("output_tokens") if isinstance(usage, dict) else None

        payload = {
            "request": _serialize_messages(messages),
            "response": _serialize_response(response),
            "error": error,
        }
        content_json = json.dumps(payload, default=str, ensure_ascii=False)

        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO llm_messages "
                    "(attempt_id, submission_id, role_bucket, provider, model, role, "
                    " content, tool_name, tool_args_json, tokens_in, tokens_out, "
                    " latency_ms, created_at) "
                    "VALUES (:aid, :sid, :rb, :prov, :model, :role, "
                    " :content, NULL, NULL, :tin, :tout, :lat, :ts)"
                ),
                {
                    "aid": attempt_id,
                    "sid": submission_id,
                    "rb": role_bucket,
                    "prov": provider_name,
                    "model": model,
                    "role": "assistant",
                    "content": content_json,
                    "tin": tokens_in,
                    "tout": tokens_out,
                    "lat": latency_ms,
                    "ts": datetime.now(timezone.utc).isoformat(),
                },
            )
