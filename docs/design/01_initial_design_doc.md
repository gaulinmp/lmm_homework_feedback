# ADA Homework Tutor — Initial Design Doc

**Status:** Draft v1 · **Author:** Mac Gaulin · **Date:** 2026-04-30

---

## Context

This document describes a homework tutor web app for the *Accounting Data Analytics* class (~200 students). The app drives a Socratic feedback loop:

1. Student picks an assignment week (e.g. "Week 3: Visualization").
2. App selects a random question from a categorized pool, biased to give one question per category before repeating any.
3. Student submits an answer in one of four formats: **text**, **graph image upload**, **Python source**, or **Excel workbook**.
4. An LLM grades the submission against a rubric and emits a verdict + Socratic feedback designed to nudge — not give — the answer.
5. Loop on steps 3–4 until the verdict is `correct`, the student abandons, or the per-question attempt cap is hit.
6. On success, the app mints a signed "proof-of-work" token. v1 just prints a receipt; a later revision will post to Canvas LMS via API.

The current repo holds only `main.py` (a LangChain hello-world hitting a local llama.cpp server running Qwen3-27B), `pyproject.toml`, and `start_server.sh`. Everything below is greenfield design.

---

## 1. Goals & non-goals

**Goals (v1)**
- Socratic feedback loop with non-spoiler tutoring.
- 4 question types: text, image, Python, Excel.
- Categorized question pool, no-repeat cycling per student.
- Per-assignment cap on credit-eligible completions.
- Proof-of-work token, verifiable via admin CLI.
- Pluggable LLM backends per role (local llama.cpp, Anthropic, OpenAI, Gemini).
- Full audit trail of every LLM message.
- Admin CLI for user management, assignment loading, token verification.

**Non-goals (v1)**
- Canvas API auto-posting (manual paste in v1; tracked in todo.md).
- University SSO (username/password only; SSO seam reserved).
- Student-supplied API keys.
- Mobile app, real-time collaboration, instructor analytics dashboard.
- Anti-cheat heuristics across submissions.
- Local execution of student code (LLM rubric check only — see §8).

## 2. System architecture

```
┌──────────┐   HTTPS   ┌───────────────────────────────────────┐
│ Browser  │ ────────► │ FastAPI (Jinja2 + HTMX + SSE)         │
│ (HTMX)   │ ◄──────── │   ├── routes/{auth,student,admin}      │
└──────────┘           │   ├── auth (argon2 + session cookie)   │
                       │   ├── LLMRouter (per-role pluggable)   │
                       │   │     ├── openai_compat → llama.cpp  │
                       │   │     ├── anthropic                  │
                       │   │     ├── openai                     │
                       │   │     └── gemini                     │
                       │   ├── LangGraph grader state machine   │
                       │   └── SQLite (WAL) — full audit trail  │
                       └───────────────────────────────────────┘
                                │
                                └─► Canvas API (stub in v1)
```

Excel files are uploaded to Anthropic via the Claude Excel skill — no local parsing in v1. Image grading goes through the `vision` LLM role bucket (cloud or local VLM, configurable).

## 3. Tech stack

| Layer | Choice |
|---|---|
| Web | FastAPI, uvicorn |
| Templates | Jinja2 + HTMX (+ a touch of vanilla JS for code editor and file upload) |
| DB | SQLite (WAL mode), SQLAlchemy Core (no ORM) |
| LLM orchestration | LangChain + LangGraph |
| Validation | Pydantic + pydantic-settings |
| Auth | argon2-cffi |
| Markdown | PyYAML for frontmatter |
| LLM SDKs | `anthropic`, `openai`, `google-generativeai` (lazy-imported per provider) |

Explicitly **not** in v1: `openpyxl`, `pandas`, `bubblewrap`, `Docker`, code-execution sandboxes, `pyodide`. These are deferred to todo.md.

## 4. Data model

SQLite, WAL mode. Eight tables. All timestamps are ISO-8601 UTC.

```sql
CREATE TABLE users (
  id              INTEGER PRIMARY KEY,
  username        TEXT UNIQUE NOT NULL,
  password_hash   TEXT NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('student','admin')),
  created_at      TEXT NOT NULL,
  canvas_user_id  TEXT
);

CREATE TABLE assignments (
  id                     INTEGER PRIMARY KEY,
  slug                   TEXT UNIQUE NOT NULL,
  week                   INTEGER,
  title                  TEXT NOT NULL,
  source_path            TEXT NOT NULL,
  frontmatter_json       TEXT NOT NULL,
  body_md                TEXT NOT NULL,
  content_hash           TEXT NOT NULL,
  max_credit_questions   INTEGER NOT NULL DEFAULT 1,
  loaded_at              TEXT NOT NULL
);

CREATE TABLE categories (
  id              INTEGER PRIMARY KEY,
  assignment_id   INTEGER NOT NULL REFERENCES assignments(id),
  name            TEXT NOT NULL,
  ordering_index  INTEGER NOT NULL,
  UNIQUE (assignment_id, name)
);

CREATE TABLE questions (
  id                       INTEGER PRIMARY KEY,
  assignment_id            INTEGER NOT NULL REFERENCES assignments(id),
  category_id              INTEGER NOT NULL REFERENCES categories(id),
  qid                      TEXT NOT NULL,
  qtype                    TEXT NOT NULL CHECK (qtype IN ('text','image','python','excel')),
  prompt_md                TEXT NOT NULL,
  rubric_md                TEXT NOT NULL,
  reference_solution_md    TEXT,
  data_files_json          TEXT NOT NULL DEFAULT '[]',
  max_attempts             INTEGER NOT NULL DEFAULT 6,
  metadata_json            TEXT NOT NULL DEFAULT '{}',
  UNIQUE (assignment_id, qid)
);

CREATE TABLE user_question_history (
  id              INTEGER PRIMARY KEY,
  user_id         INTEGER NOT NULL REFERENCES users(id),
  assignment_id   INTEGER NOT NULL REFERENCES assignments(id),
  category_id     INTEGER NOT NULL REFERENCES categories(id),
  question_id     INTEGER NOT NULL REFERENCES questions(id),
  attempt_id      INTEGER NOT NULL REFERENCES attempts(id),
  completed_at    TEXT NOT NULL
);

CREATE TABLE attempts (
  id                INTEGER PRIMARY KEY,
  user_id           INTEGER NOT NULL REFERENCES users(id),
  question_id       INTEGER NOT NULL REFERENCES questions(id),
  started_at        TEXT NOT NULL,
  completed_at      TEXT,
  status            TEXT NOT NULL CHECK (status IN ('in_progress','passed','abandoned','exhausted')),
  final_score       REAL,
  proof_token_id    INTEGER REFERENCES proof_tokens(id)
);

CREATE TABLE submissions (
  id                INTEGER PRIMARY KEY,
  attempt_id        INTEGER NOT NULL REFERENCES attempts(id),
  turn_index        INTEGER NOT NULL,
  submitted_at      TEXT NOT NULL,
  payload_kind      TEXT NOT NULL CHECK (payload_kind IN ('text','image','python','excel')),
  payload_text      TEXT,
  artifact_path     TEXT,
  grader_verdict    TEXT NOT NULL CHECK (grader_verdict IN ('correct','partial','incorrect','error')),
  grader_score      REAL,
  grader_rationale  TEXT NOT NULL,
  tutor_reply_md    TEXT
);

CREATE TABLE llm_messages (
  id              INTEGER PRIMARY KEY,
  attempt_id      INTEGER REFERENCES attempts(id),
  submission_id   INTEGER REFERENCES submissions(id),
  role_bucket     TEXT NOT NULL,    -- grader | tutor | vision | code_judge | excel_grader
  provider        TEXT NOT NULL,    -- openai_compat | anthropic | openai | gemini
  model           TEXT NOT NULL,
  role            TEXT NOT NULL,    -- system | user | assistant | tool
  content         TEXT NOT NULL,
  tool_name       TEXT,
  tool_args_json  TEXT,
  tokens_in       INTEGER,
  tokens_out      INTEGER,
  latency_ms      INTEGER,
  created_at      TEXT NOT NULL
);

CREATE TABLE proof_tokens (
  id                INTEGER PRIMARY KEY,
  attempt_id        INTEGER UNIQUE NOT NULL REFERENCES attempts(id),
  payload_json      TEXT NOT NULL,
  hmac_sig          TEXT NOT NULL,
  issued_at         TEXT NOT NULL,
  printed_at        TEXT,
  canvas_posted_at  TEXT
);
```

`llm_messages` is the audit table: every system/user/assistant/tool turn for every LLM call lands here, never deleted. This is what the admin CLI's `audit` command reads.

## 5. Assignment markdown format

One file per assignment week under `assignments/`. Top-level YAML frontmatter declares assignment metadata + ordered category list. Each question is an H2 block with its own fenced YAML config followed by the question prompt as markdown body.

```markdown
---
slug: week3_visualization
week: 3
title: "Data Visualization"
tools: ["python", "tableau"]
max_credit_questions: 5
categories:
  - name: histogram
  - name: scatter
  - name: boxplot
  - name: lineplot
---

## Question q1

```yaml
qid: q1
category: histogram
type: image
max_attempts: 6
data_files: ["data/week3_compustat_sample.csv"]
rubric: |
  - Axes labeled with units
  - Title describes the comparison
  - Histogram appropriate for distribution shape
  - Reasonable bin count or scale
reference_notes: |
  Histogram of net income across firms; expect log scale or trimmed tails.
```

Build a histogram of net income for the firms in `week3_compustat_sample.csv`. Upload your final chart as a PNG.

## Question q2
...
```

The loader (`app/assignments_loader.py`) parses frontmatter, splits on H2, parses each fenced YAML, and upserts categories + questions. `content_hash` (sha256 of the file body) is stored so re-loading skips unchanged files.

## 6. LLMRouter (pluggable per-role)

The router maps each "role bucket" (a logical purpose) to a concrete `(provider, model, base_url, api_key_env)`. Each provider is a thin wrapper around its official SDK that returns a normalized response shape and writes one row to `llm_messages`.

`config/llm.toml`:

```toml
[roles.grader]
provider = "anthropic"
model    = "claude-haiku-4-5-20251001"
api_key_env = "ANTHROPIC_API_KEY"

[roles.tutor]
provider = "openai_compat"
base_url = "http://127.0.0.1:8080/v1"
model    = "qwen3"

[roles.vision]
provider = "anthropic"
model    = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"

[roles.code_judge]
provider = "openai_compat"
base_url = "http://127.0.0.1:8080/v1"
model    = "qwen3"

[roles.excel_grader]
provider = "anthropic"        # Excel skill requires Anthropic in v1
model    = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"
```

API surface:

```python
class LLMRouter:
    def invoke(self, role: str, messages: list[dict], *, response_schema=None, files=None) -> AIMessage: ...
    async def astream(self, role: str, messages: list[dict], **kw) -> AsyncIterator[str]: ...
```

To run fully offline, change every `provider` to `openai_compat` and point `base_url` at the local llama.cpp endpoint (Excel grading then no longer works — see §8).

## 7. LangGraph grading loop

Five nodes, deterministic flow per submission:

```
prepare → preprocess → grade → tutor → persist
                                ↑ (skipped on verdict==correct)
```

1. **prepare** — load question + student artifact, dispatch by `qtype`.
2. **preprocess** — for image/excel, pass file to the right LLM role (vision or excel_grader); for text/python, no-op. **No code is ever executed.**
3. **grade** — `LLMRouter.invoke("grader", ..., response_schema=GradeVerdict)` returns `{verdict, score, rationale, weakest_concept}`.
4. **tutor** — `LLMRouter.invoke("tutor", ...)` with prior turns + verdict; streams via SSE. Skipped on `correct`.
5. **persist** — write submission, llm_messages; if passed, close attempt, mint proof token, append to `user_question_history`.

A LangGraph state machine (rather than a free-form ReAct agent) is used deliberately: predictable cost, easier debugging, and no risk of an agent talking itself into giving the answer.

## 8. Per-question-type grading paths

| Type | Path |
|---|---|
| **Text** | grader role directly |
| **Image** | vision role describes/judges the image against rubric → grader role consolidates → tutor role replies |
| **Python** | **No execution.** code_judge role receives `(rubric, student_source)` and judges whether the code "plausibly implements what the question asked." Static hints (e.g. expected import names, expected API call signatures) are encoded inside the question's `rubric` field |
| **Excel** | Student `.xlsx` is uploaded to Anthropic via the **Claude Excel skill**; excel_grader role calls Claude with the file + rubric and gets back a verdict |

**Why no Python execution?** Students in this class are accounting majors doing "vibe coding" homeworks — the goal of a Python question is to verify they implemented an LLM call or wrote pandas-like logic, not to grade output correctness. LLM rubric grading is sufficient and removes the entire sandbox attack surface.

**All-local mode caveat:** Excel grading depends on Anthropic's Excel skill. If the instructor wants to run with no cloud calls at all, Excel questions need a fallback path (an `openpyxl`-based text representation feeding the local LLM). This is tracked in `todo.md`.

## 9. Tutoring prompt design

The tutor system prompt enforces these constraints:

- "You are a tutor, not a solver. Never state the correct answer, formula, or code."
- "Identify the single most important misconception in the student's response."
- "Ask one targeted question OR point to one specific issue."
- Scaffolding intensity scales with the attempt index — light hints early, a worked analogous example with different numbers by attempt 4+.
- A post-generation guardrail (regex over `reference_solution_md` key tokens, optionally an LLM check) flags potential leakage before the reply is sent. On flag, the tutor reply is regenerated with a stricter prompt.

## 10. Web routes

| Method + path | Purpose |
|---|---|
| `GET /login`, `POST /login`, `POST /logout` | Auth |
| `GET /` | Assignment picker (list of weeks) |
| `POST /assignments/{slug}/start` | Pick category + random question, create attempt, redirect |
| `GET /attempts/{id}` | Render Socratic loop view |
| `POST /attempts/{id}/submit` | Multipart upload for files; HTMX swap with tutor reply |
| `GET /attempts/{id}/stream` | SSE token streaming for grade/tutor |
| `GET /tokens/{id}/receipt` | Printable proof-of-work receipt |
| `GET /admin/audit/{user_id\|attempt_id}` | Admin-only audit view |

## 11. Auth

- argon2-cffi for password hashing.
- Server-side session cookie backed by a `sessions` table (allows simple revocation).
- CSRF tokens on all POSTs.
- `@require_role("admin")` decorator for admin routes.
- The auth backend is hidden behind a `class AuthBackend(Protocol)` interface so a SAML/Shibboleth/CAS implementation can drop in without route changes.

## 12. Admin CLI (`cli/`)

| Tool | Purpose |
|---|---|
| `manage_users.py` | Add/list/disable/reset-password users; bulk roster CSV import |
| `load_assignments.py` | Re-parse `assignments/`, diff by content_hash, upsert |
| `verify_token.py` | Recompute HMAC of a token, print payload + ok/fail |
| `audit.py` | Dump an attempt's full LLM message log (markdown formatted) |

## 13. Concurrency

- One `asyncio.Queue` per LLM role bucket. Local roles start with `workers=1` (llama.cpp serves one request at a time); cloud roles are unbounded.
- Per-user "1 in-flight submission" lock prevents one student from monopolizing the local LLM.
- Queue position surfaced via HTMX poll + SSE.
- 60s timeout on `grade`, 120s on `tutor` (long Socratic replies can be slow on local model).
- Self-paced deadlines remove the deadline-storm risk; if hard deadlines are introduced later, the recommended fix is to switch the `grader` role to a cloud provider (one config-line change).

## 14. Security

- **No code execution** ⇒ no sandbox needed. Python submissions are stored as text and only fed to the LLM. This eliminates the largest attack surface that an exec-grading app would have.
- Upload limits: 5MB Excel, 2MB image, 100KB Python source.
- Magic-byte validation on uploads; reject mismatches (e.g. an `.xlsx` that's actually an executable or a zip bomb).
- Uploads stored under hashed paths outside the web root.
- Auth rate limit (5 attempts/min/IP); CSRF on all POSTs.
- All secrets in env vars; `.env` git-ignored, `.env.example` committed.
- All outbound calls to cloud LLM providers go over TLS; API keys never leave the server.

## 15. Proof-of-work token

HMAC-SHA256 over a canonical JSON payload, signed with a single server-side secret (`HMAC_SECRET` env var).

```json
{
  "user_id": 42,
  "username": "u6013631",
  "assignment_slug": "week3_visualization",
  "qid": "q1",
  "category": "histogram",
  "attempt_id": 1234,
  "completed_at": "2026-05-02T18:30:00Z",
  "submission_count": 3,
  "final_score": 0.95,
  "answer_hash": "sha256:..."
}
```

Token format: `base64url(payload).base64url(hmac_sig)` (a tiny JWT-shaped thing — could equivalently be PyJWT with HS256).

The receipt page renders the payload + token. Students copy the token into Canvas (the manual v1 path). The CLI `verify_token.py` recomputes the HMAC and prints OK/fail with the parsed payload.

A future `post_to_canvas(token)` function — a no-op in v1 — will replace the manual paste step.

## 16. Project layout

```
llm_homework_tutor/
├── pyproject.toml
├── .env.example
├── Makefile
├── config/
│   └── llm.toml
├── docs/
│   ├── ADA_Class_Description.md
│   └── design/
│       ├── 01_initial_design_doc.md
│       └── todo.md
├── assignments/
│   ├── week3_visualization.md
│   ├── week4_eda.md
│   └── data/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── auth.py
│   ├── routes/{auth,student,admin}.py
│   ├── templates/
│   ├── static/
│   ├── llm/
│   │   ├── router.py
│   │   ├── providers/{openai_compat,anthropic,openai,gemini}.py
│   │   ├── grader.py
│   │   ├── prompts.py
│   │   └── verdicts.py
│   ├── graders/{text,image,python_code,excel}.py
│   ├── assignments_loader.py
│   ├── picker.py
│   └── proof.py
├── cli/{manage_users,load_assignments,verify_token,audit}.py
├── data/tutor.db                   # gitignored
├── tests/
└── scripts/start_server.sh
```

**Makefile targets** (sketch):

| Target | Action |
|---|---|
| `make install` | `uv sync` |
| `make dev` | uvicorn `--reload` + llama-server in background |
| `make load` | run `cli/load_assignments.py` |
| `make user USER=foo` | admin add-user prompt |
| `make verify TOKEN=...` | verify a proof token |
| `make audit ID=...` | dump an attempt's LLM log |
| `make backup` | `sqlite3 .backup` into Dropbox folder |
| `make test` | pytest |

## 17. Deployment

- Single Linux box on the university network.
- caddy reverse-proxy → `uvicorn --workers 2 app.main:app`. Two workers because LLM calls are async-bound, not CPU-bound.
- Separate systemd unit for `llama-server` (wraps the existing `start_server.sh`).
- SQLite WAL on local SSD; nightly `sqlite3 .backup` into the Dropbox folder for free off-site backups.
- Logs to journald + a JSON sidecar of `llm_messages` (in addition to the DB).
- Llama-server port (8080) is **not** exposed externally; only the FastAPI port via caddy.
- Default `config/llm.toml` ships all-local; Mac flips role-by-role to cloud as needed.

## 18. Verification / how to test

**Unit tests**
- `assignments_loader` parses sample markdown into the expected questions + categories.
- `proof.mint` / `proof.verify` round-trip; tampered payload fails verification.
- `picker` yields one-per-category before any repeats; deterministic with a seeded RNG.
- `LLMRouter` dispatches each role to the configured provider; writes correct `llm_messages` row.
- Frontmatter validator rejects malformed assignment files.

**Integration tests**
- End-to-end attempt for each of the four question types against the configured LLMRouter (using a local model or a mocked provider in CI).
- Tutoring guardrail catches reference-solution leakage.

**Load test**
- `locust` script with 50 simulated concurrent students; verify queue UI + per-user lock behavior.

**Manual acceptance**
- Log in as a student, complete a real Week 3 image question end-to-end. Verify the receipt shows the right payload, the CLI verifier accepts the token, and `audit.py` dumps the full LLM exchange.

## 19. Future work

Tracked in `docs/design/todo.md` — see that file for the running list. Highlights: Canvas API integration, university SSO, BYO student API keys, all-local Excel fallback, local vision model, instructor analytics, RAG over course materials.

---

## Appendix A: Open questions for later iterations

These were resolved for v1 but may revisit:

1. **Tutoring guardrail**: regex-only vs second-LLM-judge. Start with regex; upgrade if leakage observed.
2. **Question authoring**: raw markdown only in v1. If TAs are added, build a validator (`make validate-assignments`) and consider a small authoring GUI.
3. **Canvas API integration timing**: out of scope for v1, but the `proof_tokens.canvas_posted_at` column and the `post_to_canvas(token)` no-op stub are wired in now to make the future migration trivial.
