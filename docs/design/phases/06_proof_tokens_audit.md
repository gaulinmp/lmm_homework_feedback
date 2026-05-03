# Phase 6 — Proof tokens & audit

## Goal
On `verdict=correct`, the app mints a signed proof-of-work token, shows a printable receipt, and exposes admin CLIs to verify a token and dump an attempt's full LLM exchange.

## Scope
**In scope:**
- `app/proof.py`:
  - `mint(attempt) -> ProofToken` — builds the canonical JSON payload from §15 (`user_id`, `username`, `assignment_slug`, `qid`, `category`, `attempt_id`, `completed_at`, `submission_count`, `final_score`, `answer_hash`), HMAC-SHA256-signs it with `HMAC_SECRET`, returns `payload.b64.sig.b64` (JWT-shaped). Inserts row into `proof_tokens` and updates `attempts.proof_token_id`.
  - `verify(token_str) -> (ok: bool, payload: dict)` — recomputes HMAC, returns parsed payload + ok flag.
  - `post_to_canvas(token) -> None` — no-op stub. Stamp `proof_tokens.canvas_posted_at` is left for the future Canvas integration.
- Wire `mint()` into the `persist` LangGraph node when `verdict==correct`.
- `GET /tokens/{id}/receipt` — Jinja-rendered receipt page showing: assignment + question, completed timestamp, full token string, payload JSON, "copy to clipboard" + "print" buttons.
- `cli/verify_token.py` — `python cli/verify_token.py TOKEN` prints OK/FAIL + parsed payload.
- `cli/audit.py` — `python cli/audit.py --attempt 1234` (or `--user demo`) reads `llm_messages` joined to `submissions`/`attempts` and prints a markdown-formatted dump: turn index, role bucket, provider, model, latency, message content. Wired into `make audit ID=...`.
- `app/routes/admin.py` — `GET /admin/audit/{user_id}` and `GET /admin/audit/attempt/{attempt_id}` rendering the same data as the CLI but in a browser. `@require_role("admin")`.
- Tests: mint/verify round-trip; tampered payload fails; tampered signature fails; missing-key audit dump returns empty cleanly.

**Out of scope:**
- Canvas API integration (todo.md).
- Token batch-submit UX (todo.md).
- Streaming/queueing/guardrails (phase 7).

## Files to create / modify
- [app/proof.py](../../../app/proof.py)
- [app/llm/grader.py](../../../app/llm/grader.py) — `persist` node calls `proof.mint` on success
- [app/routes/student.py](../../../app/routes/student.py) — `GET /tokens/{id}/receipt`
- [app/routes/admin.py](../../../app/routes/admin.py) — fill in audit views
- [app/templates/receipt.html](../../../app/templates/receipt.html), [app/templates/admin_audit.html](../../../app/templates/admin_audit.html)
- [cli/verify_token.py](../../../cli/verify_token.py)
- [cli/audit.py](../../../cli/audit.py)
- [Makefile](../../../Makefile) — wire `make verify TOKEN=...` and `make audit ID=...`
- [tests/test_proof.py](../../../tests/test_proof.py)

## Key decisions
- **HMAC-SHA256, not RSA/JWT-with-public-key.** v1 is single-instructor — there's no third party that needs to verify without the secret. Symmetric is simpler and one less key to rotate.
- **Token format = `base64url(payload).base64url(sig)`.** Effectively a tiny JWT with HS256 — could be swapped to PyJWT later with no payload changes.
- **`canvas_posted_at` column wired now, function is a no-op.** §15 + Appendix A: future Canvas integration becomes a one-function swap rather than a schema migration.
- **Audit CLI is markdown, not JSON.** It's read by humans (the instructor when a student disputes a grade), not by tools.

## Verification
- Complete a text question end-to-end → land on `/tokens/{id}/receipt` → token visible.
- `make verify TOKEN=<copied token>` prints OK + payload. Flip a character, rerun, prints FAIL.
- `make audit ID=1` prints the full LLM exchange in markdown.
- `pytest tests/test_proof.py` passes.

## Depends on
Phases 0, 1, 4 (and 5 if validating across qtypes).
