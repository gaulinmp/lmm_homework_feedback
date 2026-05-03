# Implementation phases

These docs slice the [initial design doc](../01_initial_design_doc.md) into nine ordered, demonstrable phases. Pick one up at a time and execute; each is self-contained.

| # | Phase | Hook |
|---|---|---|
| 0 | [Project skeleton & foundations](00_skeleton.md) | Layout, deps, SQLite WAL + schema, config, Makefile. Nothing else lands without this. |
| 1 | [LLMRouter & providers](01_llm_router.md) | Pluggable per-role router with `llm_messages` audit writes for every call. |
| 2 | [Assignment loader & picker](02_assignments_loader_picker.md) | Parse markdown assignments; one-per-category random picker. |
| 3 | [Auth & web skeleton](03_auth_web_skeleton.md) | argon2 login, sessions, CSRF, FastAPI + Jinja2 + HTMX shell. |
| 4 | [Grading loop (text only)](04_grading_loop_text.md) | LangGraph 5-node state machine end-to-end on text questions. |
| 5 | [Multimodal grading](05_multimodal_grading.md) | Image, Python (no exec), Excel (Anthropic skill) paths. |
| 6 | [Proof tokens & audit](06_proof_tokens_audit.md) | HMAC mint/verify, receipt page, audit + verify CLIs. |
| 7 | [Guardrails & UX polish](07_guardrails_ux_polish.md) | Leakage regex guard, SSE streaming, queue UI, per-user lock. |
| 8 | [Hardening, tests, deploy](08_hardening_deploy.md) | Full test suite, locust load test, caddy + systemd, backups. |

Phases 0–6 deliver the v1 minimum demo. Phase 7 makes it usable by ~200 students; phase 8 makes it operable.

Future work that is intentionally **not** scoped into any phase lives in [todo.md](../todo.md).
