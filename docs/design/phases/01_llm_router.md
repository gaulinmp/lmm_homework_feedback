# Phase 1 — LLMRouter & providers

## Goal
A single `LLMRouter` that routes by role bucket to one of four providers, returns a normalized response, and writes one `llm_messages` row per call — so every later LLM-shaped feature gets audit + pluggability for free.

## Scope
**In scope:**
- `config/llm.toml` shipped with the all-local default from §6 of the design doc (every role pointing at the local llama.cpp `openai_compat` backend except `excel_grader` which requires `anthropic`).
- `app/llm/router.py` — `LLMRouter.invoke(role, messages, *, response_schema=None, files=None) -> AIMessage` and `astream(...)`. Loads role→provider map from TOML at startup. Wraps every call in a try/finally that writes `llm_messages` (provider, model, role, role_bucket, content, tokens, latency).
- `app/llm/providers/openai_compat.py` — uses `langchain-openai` `ChatOpenAI` with custom `base_url` (works for llama.cpp, vLLM, OpenAI itself if base_url is the OpenAI URL).
- `app/llm/providers/anthropic.py` — `anthropic` SDK; supports `files=` for the Excel skill (used in phase 5; stub the file-upload arg now).
- `app/llm/providers/openai.py` — same as openai_compat but with the OpenAI base_url defaulted; lazy import.
- `app/llm/providers/gemini.py` — `google-generativeai` SDK; lazy import.
- `app/llm/verdicts.py` — `GradeVerdict` Pydantic model: `verdict: Literal["correct","partial","incorrect","error"]`, `score: float`, `rationale: str`, `weakest_concept: str | None`.
- `app/llm/prompts.py` — empty placeholder module (system prompts land in phase 4).
- Provider SDKs are **lazy-imported inside the provider module** so a missing API key for an unused provider doesn't crash startup.
- Unit tests against the local llama.cpp endpoint (skipped in CI if the server isn't reachable) for `openai_compat`, plus a fully-mocked test for each cloud provider.

**Out of scope:**
- Concurrency queues, per-role workers, timeouts (phase 7).
- Streaming UI plumbing (phase 7); `astream` works but isn't wired to SSE yet.
- Any actual grading logic (phase 4).

## Files to create / modify
- [config/llm.toml](../../../config/llm.toml)
- [app/llm/router.py](../../../app/llm/router.py)
- [app/llm/providers/openai_compat.py](../../../app/llm/providers/openai_compat.py)
- [app/llm/providers/anthropic.py](../../../app/llm/providers/anthropic.py)
- [app/llm/providers/openai.py](../../../app/llm/providers/openai.py)
- [app/llm/providers/gemini.py](../../../app/llm/providers/gemini.py)
- [app/llm/verdicts.py](../../../app/llm/verdicts.py)
- [app/llm/prompts.py](../../../app/llm/prompts.py) (placeholder)
- [tests/test_router.py](../../../tests/test_router.py)

## Key decisions
- **Normalized response shape = LangChain `AIMessage`.** It's already what the existing `main.py` pattern uses, and it carries `content`, `usage_metadata`, and `response_metadata` uniformly enough for the audit row.
- **One `llm_messages` row per `invoke`, not per system/user/assistant turn.** The `messages` list passed in is serialized into the `content` column as a single JSON blob. This keeps the audit table queryable by attempt+turn rather than exploding into N rows per call.
- **Structured outputs** via `response_schema=` parameter — provider modules are responsible for translating to their native shape (Anthropic tool-calling, OpenAI function/JSON mode, llama.cpp grammar). v1 may only need this for `GradeVerdict`.
- **All-local default** is the shipped config; flipping any single role to a cloud provider is a one-line TOML edit.

## Verification
- `pytest tests/test_router.py` passes; mocked tests don't need network.
- Manual: with llama.cpp running, `python -c "from app.llm.router import LLMRouter; r = LLMRouter(); print(r.invoke('grader', [{'role':'user','content':'2+2='}]).content)"` returns a sensible answer.
- After the manual call, `sqlite3 data/tutor.db 'SELECT role_bucket, provider, model, tokens_in, tokens_out, latency_ms FROM llm_messages;'` shows one row.

## Depends on
Phase 0.
