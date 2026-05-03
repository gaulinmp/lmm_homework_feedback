# Phase 5 — Multimodal grading paths

## Goal
The same LangGraph loop now handles image, Python source, and Excel submissions — covering all four qtypes per §8 of the design doc — with safe upload handling.

## Scope
**In scope:**
- `app/graders/image.py` — `preprocess` step calls `LLMRouter.invoke("vision", ...)` to describe + judge the image against the rubric; `grade` step consolidates that into a `GradeVerdict`.
- `app/graders/python_code.py` — **no execution.** `grade` step calls `LLMRouter.invoke("code_judge", ...)` with `(rubric, student_source)` and asks "does this plausibly implement what the question asked?". Prompt explicitly tells the judge it's reviewing static code, not output.
- `app/graders/excel.py` — uploads `.xlsx` to Anthropic via the Claude Excel skill in `LLMRouter.invoke("excel_grader", ..., files=[xlsx_path])`. The provider module's `files=` arg (stubbed in phase 1) gets its real implementation here.
- Upload validation in `app/routes/student.py` `POST /attempts/{id}/submit`:
  - Size caps per qtype: 5MB Excel, 2MB image, 100KB Python source.
  - Magic-byte check (use `python-magic` or hand-rolled signatures): xlsx must be PK ZIP starting `PK\x03\x04`; image must be PNG/JPEG signature; python is text/utf-8.
  - Reject mismatches (e.g. an `.xlsx` that's actually an executable or a zip-bomb-shaped file).
  - Storage path: `data/uploads/<sha256[:2]>/<sha256>.<ext>`, written before grading; the path goes into `submissions.artifact_path`.
  - Uploads dir is **outside** the static mount.
- Dispatch in `prepare`: switch on `question.qtype` to pick the right grader module.
- Add real questions of each qtype to `week3_visualization.md` (one image, one python, one excel — alongside the existing text one from phase 4).
- Tests: per-qtype unit tests against the LangGraph with a mocked router; magic-byte rejection test for each qtype.

**Out of scope:**
- All-local Excel fallback via openpyxl (todo.md).
- Local vision model (todo.md — current default is Anthropic per `config/llm.toml`).
- Streaming, guardrails, queueing (phase 7).

## Files to create / modify
- [app/graders/image.py](../../../app/graders/image.py)
- [app/graders/python_code.py](../../../app/graders/python_code.py)
- [app/graders/excel.py](../../../app/graders/excel.py)
- [app/llm/grader.py](../../../app/llm/grader.py) — extend `prepare`/`preprocess` dispatch
- [app/llm/providers/anthropic.py](../../../app/llm/providers/anthropic.py) — implement `files=` (Excel skill upload)
- [app/routes/student.py](../../../app/routes/student.py) — multipart parsing, validation, hashed-path storage
- [assignments/week3_visualization.md](../../../assignments/week3_visualization.md) — add image/python/excel questions
- [tests/test_graders_multimodal.py](../../../tests/test_graders_multimodal.py)
- [tests/test_uploads.py](../../../tests/test_uploads.py)

## Key decisions
- **No code execution. Ever.** §8 + §14: students are accounting majors doing "vibe coding," and removing the sandbox eliminates the largest possible attack surface. The `code_judge` role inspects source statically.
- **Excel grading requires Anthropic in v1.** `config/llm.toml` already pins `roles.excel_grader = anthropic`. If the instructor flips everything to local, Excel questions will fail loudly — the all-local fallback is explicitly punted to todo.md.
- **Hashed-path storage prevents directory enumeration** and dedupes byte-identical uploads automatically.
- **Magic-byte check is mandatory, not extension trust.** Browsers and curl will happily send anything.

## Verification
- Manual: submit a real PNG histogram for the image question → vision role describes it → grader returns a verdict that references rubric criteria.
- Manual: submit a Python file → code_judge returns a verdict without executing anything (verify by `ps`/inspection — no python interpreter spawned).
- Manual: submit an `.xlsx` → uploaded to Anthropic via Excel skill → verdict returns.
- Negative: rename `evil.exe` to `submission.xlsx`, upload — gets 400 with magic-byte error.
- `pytest tests/test_graders_multimodal.py tests/test_uploads.py` passes.

## Depends on
Phases 0, 1, 2, 3, 4.
