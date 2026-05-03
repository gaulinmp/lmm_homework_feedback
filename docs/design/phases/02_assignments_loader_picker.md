# Phase 2 — Assignment loader & picker

## Goal
Parse markdown assignments into the `assignments` / `categories` / `questions` tables, and pick the next question for a given (user, assignment) so that every category is seen once before any repeats.

## Scope
**In scope:**
- `app/assignments_loader.py`:
  - Parse top-level YAML frontmatter (PyYAML).
  - Split body on `## Question` H2 headers.
  - For each question block, parse its fenced ` ```yaml ` config, treat the rest as `prompt_md`.
  - Compute sha256 of the file body as `content_hash`.
  - Upsert `assignments` (by `slug`); skip if `content_hash` unchanged.
  - Upsert `categories` (by `(assignment_id, name)`) preserving `ordering_index` from frontmatter.
  - Upsert `questions` (by `(assignment_id, qid)`).
  - Validate qtype ∈ {text, image, python, excel} and that `category` references a declared category. Raise on schema violations.
- `app/picker.py`:
  - `pick_next_question(user_id, assignment_id, *, rng=None) -> Question` — selects a random question from the assignment, biased so categories not yet completed by this user are picked first; within an unseen category, picks a random question; falls back to repeats once all categories are exhausted.
  - "Completed" = present in `user_question_history` for this `(user_id, assignment_id)`.
  - Deterministic with a seeded `rng` for tests.
- `cli/load_assignments.py` — argparse-driven CLI: scans `assignments/*.md`, calls the loader, prints diff (added/updated/skipped). Wired into `make load`.
- `assignments/week3_visualization.md` — one real assignment file matching the schema in §5, with 4 categories × 2 questions. Use the example block in §5 as q1.
- Unit tests for loader (parses sample → expected rows; rejects malformed) and picker (one-per-category-before-repeat with seeded RNG; falls back gracefully when categories are exhausted).

**Out of scope:**
- Any web routes (phase 3+).
- Authoring GUI / `make validate-assignments` (todo.md).

## Files to create / modify
- [app/assignments_loader.py](../../../app/assignments_loader.py)
- [app/picker.py](../../../app/picker.py)
- [cli/load_assignments.py](../../../cli/load_assignments.py)
- [assignments/week3_visualization.md](../../../assignments/week3_visualization.md)
- [tests/test_assignments_loader.py](../../../tests/test_assignments_loader.py)
- [tests/test_picker.py](../../../tests/test_picker.py)
- [Makefile](../../../Makefile) — wire `make load`

## Key decisions
- **Content-hash skip** is at the file level, not the question level. If any question changes, the whole file re-upserts; this is acceptable for v1 since the loader is idempotent anyway.
- **Picker biases to unseen categories, not strict round-robin.** This handles the case where an instructor adds a new category mid-semester — students who've completed every old category just see the new category come up next.
- **Loader is invoked from a CLI, not on FastAPI startup.** Keeps boot fast and makes "did the load succeed?" observable.

## Verification
- `python cli/load_assignments.py` on a fresh DB inserts 1 assignment, 4 categories, 8 questions; rerun with no changes prints "skipped". Touch the file, rerun, prints "updated".
- `pytest tests/test_assignments_loader.py tests/test_picker.py` passes.
- `sqlite3 data/tutor.db 'SELECT slug, week, title FROM assignments;'` shows the loaded row.

## Depends on
Phase 0.
