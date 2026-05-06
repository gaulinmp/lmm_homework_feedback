from __future__ import annotations

import os
from typing import Any, AsyncIterator

from langchain_core.messages import AIMessage, BaseMessage


def _flatten(messages) -> str:
    parts: list[str] = []
    for m in messages:
        if isinstance(m, BaseMessage):
            role = m.type
            content = m.content
        else:
            role = m.get("role", "user")
            content = m.get("content", "")
        if not isinstance(content, str):
            content = str(content)
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        model: str,
        api_key_env: str = "GOOGLE_API_KEY",
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.api_key = api_key
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            import google.generativeai as genai  # lazy

            key = self.api_key or os.environ.get(self.api_key_env)
            genai.configure(api_key=key)
            self._client = genai.GenerativeModel(self.model)
        return self._client

    def invoke(
        self,
        messages,
        *,
        response_schema=None,
        files=None,
    ) -> AIMessage:
        del files
        client = self._ensure_client()
        prompt = _flatten(messages)
        gen_config: dict[str, Any] | None = None
        if response_schema is not None:
            schema = (
                response_schema.model_json_schema()
                if hasattr(response_schema, "model_json_schema")
                else response_schema
            )
            gen_config = {"response_mime_type": "application/json", "response_schema": schema}
        resp = (
            client.generate_content(prompt, generation_config=gen_config)
            if gen_config
            else client.generate_content(prompt)
        )
        usage_meta = None
        usage = getattr(resp, "usage_metadata", None)
        if usage is not None:
            in_tok = getattr(usage, "prompt_token_count", 0) or 0
            out_tok = getattr(usage, "candidates_token_count", 0) or 0
            usage_meta = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "total_tokens": getattr(usage, "total_token_count", in_tok + out_tok),
            }
        return AIMessage(content=resp.text, usage_metadata=usage_meta)

    async def astream(
        self,
        messages,
        *,
        response_schema=None,
        files=None,
    ) -> AsyncIterator[str]:
        # google-generativeai's streaming is sync; for phase 1 we yield the whole reply.
        ai = self.invoke(messages, response_schema=response_schema, files=files)
        yield ai.content
