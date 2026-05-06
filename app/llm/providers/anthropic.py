from __future__ import annotations

import os
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage, BaseMessage


def _split_system(messages) -> tuple[str | None, list[dict[str, Any]]]:
    """Anthropic wants `system=` separate from the messages list."""
    system_parts: list[str] = []
    user_msgs: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, BaseMessage):
            role = "user" if m.type == "human" else m.type
            content = m.content
        else:
            role = m.get("role", "user")
            content = m.get("content", "")
        if role == "system":
            system_parts.append(content if isinstance(content, str) else str(content))
        else:
            user_msgs.append(
                {"role": "assistant" if role in ("assistant", "ai") else "user", "content": content}
            )
    system = "\n\n".join(system_parts) if system_parts else None
    return system, user_msgs


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        model: str,
        api_key_env: str = "ANTHROPIC_API_KEY",
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.api_key = api_key
        self.base_url = base_url
        self.max_tokens = max_tokens
        self._client = None
        self._aclient = None

    def _resolve_key(self) -> str | None:
        return self.api_key or os.environ.get(self.api_key_env)

    def _ensure_client(self):
        if self._client is None:
            import anthropic  # lazy

            kwargs: dict[str, Any] = {"api_key": self._resolve_key()}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _ensure_aclient(self):
        if self._aclient is None:
            import anthropic  # lazy

            kwargs: dict[str, Any] = {"api_key": self._resolve_key()}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._aclient = anthropic.AsyncAnthropic(**kwargs)
        return self._aclient

    def invoke(
        self,
        messages,
        *,
        response_schema=None,
        files=None,
    ) -> AIMessage:
        # `files=` is a stub for the phase 5 Excel skill; not used yet.
        del files
        client = self._ensure_client()
        system, user_msgs = _split_system(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": user_msgs,
        }
        if system is not None:
            kwargs["system"] = system

        if response_schema is not None:
            schema = (
                response_schema.model_json_schema()
                if hasattr(response_schema, "model_json_schema")
                else response_schema
            )
            kwargs["tools"] = [
                {
                    "name": "respond",
                    "description": "Return a structured response.",
                    "input_schema": schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "respond"}

        resp = client.messages.create(**kwargs)

        text_parts: list[str] = []
        parsed: Any = None
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                parsed = block.input

        usage = getattr(resp, "usage", None)
        usage_meta = None
        if usage is not None:
            in_tok = getattr(usage, "input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            usage_meta = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": in_tok + out_tok,
            }

        ai = AIMessage(
            content="".join(text_parts),
            usage_metadata=usage_meta,
            response_metadata={
                "model": getattr(resp, "model", self.model),
                "stop_reason": getattr(resp, "stop_reason", None),
            },
        )
        if parsed is not None:
            if hasattr(response_schema, "model_validate"):
                try:
                    ai.additional_kwargs["parsed"] = response_schema.model_validate(parsed)
                except Exception:
                    ai.additional_kwargs["parsed"] = parsed
            else:
                ai.additional_kwargs["parsed"] = parsed
        return ai

    async def astream(
        self,
        messages,
        *,
        response_schema=None,
        files=None,
    ) -> AsyncIterator[str]:
        del response_schema, files
        client = self._ensure_aclient()
        system, user_msgs = _split_system(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": user_msgs,
        }
        if system is not None:
            kwargs["system"] = system
        async with client.messages.stream(**kwargs) as stream:
            async for piece in stream.text_stream:
                yield piece
