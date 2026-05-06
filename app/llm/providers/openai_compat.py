from __future__ import annotations

import os
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage


class OpenAICompatProvider:
    """OpenAI-compatible chat provider (works for llama.cpp, vLLM, OpenAI itself)."""

    name = "openai_compat"

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key_env: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.temperature = temperature
        if api_key:
            self.api_key = api_key
        elif api_key_env:
            self.api_key = os.environ.get(api_key_env, "not-needed")
        else:
            self.api_key = "not-needed"
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from langchain_openai import ChatOpenAI  # lazy

            kwargs: dict[str, Any] = {
                "model": self.model,
                "api_key": self.api_key,
                "temperature": self.temperature,
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = ChatOpenAI(**kwargs)
        return self._client

    def invoke(
        self,
        messages,
        *,
        response_schema=None,
        files=None,
    ) -> AIMessage:
        client = self._ensure_client()
        if response_schema is not None:
            structured = client.with_structured_output(response_schema, include_raw=True)
            result = structured.invoke(messages)
            raw = result["raw"] if isinstance(result, dict) else result
            if not isinstance(raw, AIMessage):
                raw = AIMessage(content=str(raw))
            if isinstance(result, dict):
                raw.additional_kwargs["parsed"] = result.get("parsed")
            return raw
        return client.invoke(messages)

    async def astream(
        self,
        messages,
        *,
        response_schema=None,
        files=None,
    ) -> AsyncIterator[str]:
        client = self._ensure_client()
        async for chunk in client.astream(messages):
            content = getattr(chunk, "content", "")
            if content:
                yield content
