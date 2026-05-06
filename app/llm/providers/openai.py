from __future__ import annotations

from .openai_compat import OpenAICompatProvider


class OpenAIProvider(OpenAICompatProvider):
    """OpenAI cloud provider — same machinery as openai_compat with the default base URL."""

    name = "openai"

    def __init__(
        self,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        api_key: str | None = None,
        temperature: float = 0.0,
    ) -> None:
        super().__init__(
            model=model,
            base_url=None,
            api_key_env=api_key_env,
            api_key=api_key,
            temperature=temperature,
        )
