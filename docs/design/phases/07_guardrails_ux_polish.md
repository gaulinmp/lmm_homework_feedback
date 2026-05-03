# Phase 7 — Guardrails & UX polish

## Goal
The tutor never leaks the reference solution, replies stream token-by-token, the local LLM doesn't get DOSed by one student, and ~200 students can use the system simultaneously without it falling over.

## Scope
**In scope:**
- **Tutor leakage guardrail** in `app/llm/grader.py` `tutor` node:
  - After tutor reply is generated, run a regex over `reference_solution_md` extracting key tokens (numbers, variable names, signatures) and check the reply doesn't contain them verbatim.
  - On flag: regenerate once with a stricter prompt that explicitly names the leaked token. If still flagged, fall back to a canned "Let me try a different approach — what part of the rubric do you find most confusing?" reply and log the leak.
  - Optional second-LLM judge upgrade is **deferred to todo.md**.
- **SSE streaming**: `GET /attempts/{id}/stream` server-sent-events endpoint. Tutor replies are streamed via `LLMRouter.astream`; the `_turn.html` partial uses `hx-ext="sse"` to swap tokens in. The `submit` POST returns a placeholder turn that opens an SSE connection.
- **Per-role asyncio.Queue** in `app/llm/router.py`: each role bucket has a queue with `workers` workers. Local roles default `workers=1` (llama.cpp serves one at a time); cloud roles unbounded.
- **Per-user in-flight lock**: an `asyncio.Lock` keyed by `user_id` prevents one student from submitting twice while their first submission is still grading.
- **Queue-position UI**: HTMX poll endpoint `GET /attempts/{id}/queue-status` returns "you're #N in line for the tutor"; rendered above the form while a submission is in flight.
- **Timeouts**: 60s on `grade`, 120s on `tutor`. On timeout: surface a polite error to the student, mark the submission `grader_verdict='error'`, do not consume an attempt.
- Tests: leakage regex catches a synthesized leak; SSE endpoint streams; queue lock prevents double-submit; timeout surfaces correctly.

**Out of scope:**
- LLM-judge guardrail upgrade (todo.md).
- Anti-cheat / dedupe (todo.md).
- Dark mode (todo.md).

## Files to create / modify
- [app/llm/grader.py](../../../app/llm/grader.py) — guardrail in `tutor` node
- [app/llm/router.py](../../../app/llm/router.py) — per-role queues, per-user locks, timeouts
- [app/routes/student.py](../../../app/routes/student.py) — SSE endpoint, queue-status endpoint
- [app/templates/_turn.html](../../../app/templates/_turn.html), [attempt.html](../../../app/templates/attempt.html) — `hx-ext="sse"` wiring, queue-position banner
- [app/static/style.css](../../../app/static/style.css) — streaming cursor, queue banner
- [tests/test_guardrails.py](../../../tests/test_guardrails.py), [tests/test_streaming.py](../../../tests/test_streaming.py), [tests/test_queue.py](../../../tests/test_queue.py)

## Key decisions
- **Regex guardrail before LLM-judge.** §9 + Appendix A: start cheap, upgrade only if leakage observed.
- **Per-user lock is in-process.** v1 runs `--workers 2` of uvicorn; if a user races between workers they get serialized at the DB layer (the `attempts` row's `status='in_progress'` is the cross-process source of truth). Document this; don't over-engineer to Redis.
- **One worker for local roles** is a deliberate throttle, not a limitation — llama.cpp only serves one request at a time anyway, and the queue position UX makes the wait visible.
- **Self-paced removes deadline storms.** §13: if hard deadlines are added later, the documented fix is to flip `roles.grader` to a cloud provider via TOML.

## Verification
- Manual: synthesize a leak (put the rubric's reference number directly into the tutor system prompt) → guardrail catches it, regenerates, doesn't leak.
- Manual: submit two attempts in two browser tabs as the same user → second one queues with visible position, doesn't double-charge attempts.
- Manual: tutor reply streams visibly token-by-token in the browser.
- `pytest tests/test_guardrails.py tests/test_streaming.py tests/test_queue.py` passes.

## Depends on
Phases 0–6.
