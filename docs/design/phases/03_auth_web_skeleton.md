# Phase 3 — Auth & web skeleton

## Goal
A logged-in student can browse to the assignment picker page; admins can hit admin-gated routes. Login/logout works, sessions persist across restarts, and a `cli/manage_users.py` lets the instructor add students from a roster CSV.

## Scope
**In scope:**
- `app/auth.py`:
  - `class AuthBackend(Protocol)` with `authenticate(username, password) -> User | None` and `create_user(...)`.
  - `class LocalAuthBackend(AuthBackend)` — argon2-cffi over the `users` table.
  - `Session` table-backed cookies (server-side; supports revocation). Cookie is HTTP-only, SameSite=Lax, Secure when `ENV=prod`.
  - CSRF middleware: per-session token, required on every non-GET; injected into Jinja templates as `csrf_token`.
  - `@require_role("admin")` and `@require_login` dependencies.
  - Auth rate limit: 5 failed attempts per minute per IP (in-memory leaky bucket is fine for v1; one box).
- `app/routes/auth.py` — `GET/POST /login`, `POST /logout`.
- `app/routes/student.py` — `GET /` assignment picker (lists weeks); placeholder `POST /assignments/{slug}/start` returning "not yet implemented" — phase 4 fills it in.
- `app/routes/admin.py` — placeholder `GET /admin/audit/{...}`; phase 6 fills it in.
- `app/templates/base.html`, `login.html`, `picker.html` (Jinja2 + HTMX `<script>` from CDN, vanilla CSS).
- `app/static/` — minimal stylesheet.
- `cli/manage_users.py` — `add`, `list`, `disable`, `reset-password`, `import-csv` subcommands. Wired into `make user USER=foo`.
- Tests: argon2 round-trip, login/logout flow against `httpx` test client, CSRF rejection on missing token, rate limit triggers after 5 attempts.

**Out of scope:**
- The grading loop and submission UI (phase 4).
- SSO / Shibboleth (todo.md) — but `AuthBackend` Protocol is the seam.

## Files to create / modify
- [app/auth.py](../../../app/auth.py)
- [app/routes/auth.py](../../../app/routes/auth.py)
- [app/routes/student.py](../../../app/routes/student.py)
- [app/routes/admin.py](../../../app/routes/admin.py)
- [app/templates/base.html](../../../app/templates/base.html), [login.html](../../../app/templates/login.html), [picker.html](../../../app/templates/picker.html)
- [app/static/style.css](../../../app/static/style.css)
- [app/main.py](../../../app/main.py) — register routers, mount static, install middleware
- [cli/manage_users.py](../../../cli/manage_users.py)
- [tests/test_auth.py](../../../tests/test_auth.py)

## Key decisions
- **Session table over JWT.** Revocation matters (account compromise, password reset); JWT makes that hard without a denylist anyway.
- **CSRF token in template context, not double-submit cookie.** Simpler with Jinja, fine for HTMX which has `hx-headers` for the token.
- **Rate limit is in-memory.** v1 is one box; if later sharded, swap to Redis. Don't pre-build that now.

## Verification
- `python cli/manage_users.py add --username demo --role student` prompts for password, creates the row.
- Browse to `/login`, log in as demo, land on `/`, see Week 3 in the assignment list.
- `httpx` test client: full login → GET / → logout → GET / redirects back to /login.
- `pytest tests/test_auth.py` passes.

## Depends on
Phases 0, 2.
