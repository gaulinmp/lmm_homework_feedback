# Phase 4 — Grading loop (text only)

## Goal
A logged-in student can pick an assignment, get a random text question, submit a written answer, see a Socratic tutor reply, and iterate until verdict=correct or attempts are exhausted — proving the LangGraph state machine end-to-end on the simplest qtype.

## Scope
**In scope:**
- `app/llm/grader.py` — LangGraph state machine with the 5 nodes from §7: `prepare → preprocess → grade → tutor → persist`. State is a Pydantic model carrying `attempt`, `question`, `submission_payload`, `verdict`, `tutor_reply`, `turn_index`.
- `app/graders/text.py` — text grader prompt builder; takes `(question, rubric, student_text, prior_turns)` and produces the message list passed to `LLMRouter.invoke("grader", ..., response_schema=GradeVerdict)`.
- `app/llm/prompts.py` — fill in: tutor system prompt enforcing the §9 constraints (no answer reveal, single-misconception focus, attempt-indexed scaffolding intensity).
- Routes wired in:
  - `POST /assignments/{slug}/start` — calls `picker.pick_next_question`, creates `attempts` row, redirects to `/attempts/{id}`.
  - `GET /attempts/{id}` — renders question prompt + submission form + prior-turns transcript.
  - `POST /attempts/{id}/submit` — multipart-aware (text-only for now); runs the LangGraph; HTMX-swaps the new transcript turn.
- Templates: `attempt.html` (transcript + form), partial `_turn.html` for HTMX swaps.
- On `verdict==correct`: close the `attempts` row (`status=passed`), append `user_question_history` row. Proof token stays as a TODO comment until phase 6 — stamp `proof_token_id=NULL` for now.
- On `attempts >= max_attempts`: close attempt with `status=exhausted`.
- Tests: full graph run with a mocked `LLMRouter` that returns a scripted verdict sequence (incorrect → partial → correct), assert correct DB rows and tutor-skipped-on-correct behavior.

**Out of scope:**
- Image, Python, Excel grading (phase 5).
- Tutor leakage guardrails (phase 7).
- SSE streaming — for now the route blocks until `tutor` returns and renders the full reply (phase 7 streams it).
- Per-user concurrency lock (phase 7).
- Proof-of-work token (phase 6).

## Files to create / modify
- [app/llm/grader.py](../../../app/llm/grader.py)
- [app/graders/text.py](../../../app/graders/text.py)
- [app/graders/__init__.py](../../../app/graders/__init__.py)
- [app/llm/prompts.py](../../../app/llm/prompts.py)
- [app/routes/student.py](../../../app/routes/student.py) — fill in start/view/submit
- [app/templates/attempt.html](../../../app/templates/attempt.html), [app/templates/_turn.html](../../../app/templates/_turn.html)
- [tests/test_grading_loop.py](../../../tests/test_grading_loop.py)

## Key decisions
- **LangGraph, not free-form ReAct.** §7 calls this out: deterministic cost, no agent talking itself into giving the answer.
- **`tutor` node is skipped on `correct`.** No reason to spend tokens explaining a right answer.
- **`prepare` and `preprocess` exist even though text doesn't need them.** Keeps the graph shape constant across qtypes; phase 5 fills them with real work for image/excel.
- **Block-then-render this phase, stream in phase 7.** Streaming infrastructure is its own complexity; getting the loop correct first is more important.

## Verification
- Manual: log in as demo, start week3, the picker returns a text question (add a synthetic text question to `week3_visualization.md` for this phase), submit a wrong answer, get a Socratic reply that doesn't include the answer, submit a right answer, attempt closes with `status=passed`.
- `sqlite3 data/tutor.db 'SELECT turn_index, payload_kind, grader_verdict FROM submissions WHERE attempt_id=1;'` shows the turn-by-turn history.
- `pytest tests/test_grading_loop.py` passes with mocked router.

## Depends on
Phases 0, 1, 2, 3.
