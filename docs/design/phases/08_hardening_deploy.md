# Phase 8 — Hardening, tests, deployment

## Goal
The app runs as a managed service on the university Linux box behind caddy, survives a 50-student locust test, and has a backup story.

## Scope
**In scope:**
- **Full unit + integration test pass per §18:**
  - `assignments_loader` fixtures (already in phase 2).
  - `proof.mint`/`verify` round-trip (phase 6).
  - `picker` deterministic with seeded RNG (phase 2).
  - `LLMRouter` per-role dispatch + audit row (phase 1).
  - Frontmatter validator rejects malformed files (phase 2).
  - End-to-end attempt for each of the four qtypes against a mocked LLMRouter.
  - Tutoring guardrail catches a synthesized leak (phase 7).
  - Wire `make test` to run the whole suite.
- **Locust load test** in `scripts/locust_students.py`: 50 simulated concurrent students hitting the picker + submitting; assert latency p95 + zero 5xx + queue-position UI updates correctly.
- **Deployment**:
  - `scripts/start_server.sh` updated for `uvicorn --workers 2 app.main:app`.
  - `scripts/llama_server.sh` — separate launcher (refactor of the existing root-level `start_server.sh`).
  - `deploy/caddyfile` — reverse-proxy config; only the FastAPI port exposed externally, llama-server bound to 127.0.0.1:8080.
  - `deploy/tutor-app.service`, `deploy/tutor-llama.service` — systemd units for both processes.
  - `scripts/backup.sh` — `sqlite3 .backup` to `~/Dropbox/llm_homework_tutor_backups/$(date)`. Wired into a nightly cron line documented in deploy README.
- **Logging**:
  - Configure structured JSON logging to journald.
  - Sidecar JSON appender for `llm_messages` rows (in addition to the DB) for offline analysis.
- **Manual acceptance** per §18: log in as a real student account, complete a Week 3 image question end-to-end, verify receipt + token verifier + audit dump all work.
- `deploy/README.md` — the actual run-book: how to install systemd units, where backups go, how to roll an LLM config change without restarting llama-server.

**Out of scope:**
- Anything in `todo.md` (Canvas API, SSO, BYO keys, all-local Excel, instructor analytics, etc.). Those are tracked separately and shipped as follow-ups.

## Files to create / modify
- [tests/](../../../tests/) — fill out remaining suites
- [scripts/locust_students.py](../../../scripts/locust_students.py)
- [scripts/start_server.sh](../../../scripts/start_server.sh) (refactor existing)
- [scripts/llama_server.sh](../../../scripts/llama_server.sh)
- [scripts/backup.sh](../../../scripts/backup.sh)
- [deploy/caddyfile](../../../deploy/caddyfile)
- [deploy/tutor-app.service](../../../deploy/tutor-app.service)
- [deploy/tutor-llama.service](../../../deploy/tutor-llama.service)
- [deploy/README.md](../../../deploy/README.md)
- [app/main.py](../../../app/main.py) — JSON logging config
- [Makefile](../../../Makefile) — wire `make test`, `make backup`, `make load-test`

## Key decisions
- **Two uvicorn workers, not one.** §17: LLM calls are async-bound, not CPU-bound; two workers gives free failover if one dies.
- **Llama-server bound to localhost.** §14 + §17: never expose the model server externally, even for "internal testing."
- **SQLite `.backup` to Dropbox.** §17: free off-site backups, no extra infra. Document the recovery procedure in `deploy/README.md`.
- **No CI infrastructure in v1.** Tests run via `make test` on the dev machine and on the prod box pre-restart. CI (GitHub Actions, etc.) is a follow-up if/when this codebase is opened up to TAs.

## Verification
- `make test` — full suite green.
- `make load-test` — locust against staging-mode (mocked LLM); p95 < 500ms for non-LLM routes; zero 5xx; queue UI updates correctly under load.
- `systemctl status tutor-app tutor-llama` — both green after install.
- End-to-end manual: real student walks through one Week 3 image question; `make audit ID=...` shows the full exchange.
- Backup: run `make backup`, verify a `.db` file appears in the Dropbox folder; verify restore by pointing a fresh checkout at the backup path.

## Depends on
Phases 0–7.
