# Phase 0 — Project skeleton & foundations

## Goal
A runnable empty FastAPI app, configured via env + TOML, backed by an empty SQLite DB whose schema matches §4 of the design doc — the substrate every later phase plugs into.

## Scope
**In scope:**
- `pyproject.toml` updated with v1 deps: `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `sqlalchemy`, `pydantic`, `pydantic-settings`, `argon2-cffi`, `pyyaml`, `tomli`, `langchain`, `langgraph`, `langchain-openai`, `anthropic`, `httpx`. Dev deps: `pytest`, `pytest-asyncio`, `httpx` (test client).
- Project layout per §16: `app/`, `app/routes/`, `app/llm/`, `app/llm/providers/`, `app/graders/`, `app/templates/`, `app/static/`, `assignments/data/`, `cli/`, `tests/`, `scripts/`, `config/`, `data/`.
- `app/config.py` — `pydantic-settings` reading `.env` + `config/llm.toml`. Settings: `DATA_DIR`, `DB_PATH`, `HMAC_SECRET`, `SESSION_SECRET`, `LLM_CONFIG_PATH`, upload caps.
- `app/db.py` — SQLAlchemy Core engine, WAL pragma on connect, `init_db()` that creates all 8 tables from §4 (plus a `sessions` table for auth, used in phase 3).
- `app/main.py` — FastAPI app with one health route `GET /healthz`.
- `.env.example`, `.gitignore` (ignore `data/`, `.env`, `.venv`, `__pycache__`).
- `Makefile` with at least `install`, `dev`, `test`, `backup` targets (others stubbed — they'll wire up in later phases).
- Git init + initial commit.

**Out of scope:**
- LLM calls (phase 1), auth (phase 3), routes beyond healthz, any UI, any CLI tools.

## Files to create / modify
- [pyproject.toml](../../../pyproject.toml) — replace
- [app/config.py](../../../app/config.py)
- [app/db.py](../../../app/db.py)
- [app/main.py](../../../app/main.py) — replaces existing root `main.py` hello-world (move that to `scripts/llama_smoketest.py` or delete)
- [.env.example](../../../.env.example), [.gitignore](../../../.gitignore), [Makefile](../../../Makefile)
- All directory placeholders (empty `__init__.py` where needed)

## Key decisions
- **SQLAlchemy Core, not ORM.** Per §3 — keeps queries explicit and trivial to audit.
- **Schema lives as plain `CREATE TABLE` strings in `db.py`**, not Alembic. v1 has one author and one box; migration tooling is overkill until the schema starts to evolve in production.
- **WAL mode set per-connection** via `PRAGMA journal_mode=WAL` in a SQLAlchemy `connect` event listener.
- **No code in `main.py` beyond app instantiation + healthz.** Routes get added in their own phases.

## Verification
- `make install` succeeds.
- `make dev` starts the server; `curl localhost:8000/healthz` returns 200.
- `python -c "from app.db import init_db; init_db()"` creates `data/tutor.db`; `sqlite3 data/tutor.db '.tables'` lists all 9 tables (8 from §4 + `sessions`).
- `pytest` runs (no tests yet — passes trivially or with one schema-creation test).

## Depends on
None.
