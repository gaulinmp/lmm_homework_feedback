"""Phase 4 — grading loop tests with a mocked LLMRouter.

The router is replaced with a scripted fake that emits a sequence of verdicts
(incorrect → partial → correct). We assert:

- the LangGraph state machine runs cleanly end to end
- each turn writes a `submissions` row with the right verdict and rationale
- the tutor node is skipped on `correct` (no tutor_reply_md persisted)
- the attempt closes with `status='passed'` on correct
- a `user_question_history` row is appended on success
- max-attempts exhaustion closes the attempt with `status='exhausted'`
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.auth import LocalAuthBackend, auth_rate_limit, csrf_token_for
from app.llm.grader import GraderState, build_grader_graph
from app.llm.verdicts import GradeVerdict
from app.main import app
from app.routes import student as student_routes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_engine() -> Engine:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fks(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    with engine.begin() as conn:
        for stmt in db_module.SCHEMA_STATEMENTS:
            conn.exec_driver_sql(stmt)
    return engine


@pytest.fixture
def engine(monkeypatch) -> Engine:
    e = _make_engine()
    monkeypatch.setattr(db_module, "_engine", e)
    auth_rate_limit.reset()
    return e


@pytest.fixture
def seeded(engine):
    """Insert a user, an assignment with one text question, return ids."""
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('alice', 'x', 'student', :c)"
            ),
            {"c": now},
        ).lastrowid
        assignment_id = conn.execute(
            text(
                "INSERT INTO assignments "
                "(slug, week, title, source_path, frontmatter_json, body_md, "
                " content_hash, max_credit_questions, loaded_at) "
                "VALUES ('demo', 1, 'Demo', 'x', '{}', '', 'h', 1, :c)"
            ),
            {"c": now},
        ).lastrowid
        cat_id = conn.execute(
            text(
                "INSERT INTO categories (assignment_id, name, ordering_index) "
                "VALUES (:a, 'cat', 0)"
            ),
            {"a": assignment_id},
        ).lastrowid
        question_id = conn.execute(
            text(
                "INSERT INTO questions "
                "(assignment_id, category_id, qid, qtype, prompt_md, rubric_md, "
                " max_attempts) "
                "VALUES (:a, :c, 'q1', 'text', "
                " 'Explain why a histogram is informative.', "
                " '- mentions distribution shape\n- mentions outliers', 3)"
            ),
            {"a": assignment_id, "c": cat_id},
        ).lastrowid
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempts "
                "(user_id, question_id, started_at, status) "
                "VALUES (:u, :q, :s, 'in_progress')"
            ),
            {"u": user_id, "q": question_id, "s": now},
        ).lastrowid
    return {
        "user_id": user_id,
        "assignment_id": assignment_id,
        "category_id": cat_id,
        "question_id": question_id,
        "attempt_id": attempt_id,
    }


class ScriptedRouter:
    """A pretend LLMRouter that returns a fixed sequence of grader verdicts.

    The tutor role just returns a stub Socratic reply. ``invoke`` writes
    nothing to llm_messages — that's the real router's job and is exercised
    in test_router.py.
    """

    def __init__(self, verdicts: list[GradeVerdict]):
        self.verdicts = list(verdicts)
        self.calls: list[tuple[str, list]] = []

    def invoke(self, role, messages, *, response_schema=None, files=None,
               attempt_id=None, submission_id=None):
        self.calls.append((role, messages))
        if role == "grader":
            v = self.verdicts.pop(0)
            return AIMessage(content="", additional_kwargs={"parsed": v})
        if role == "tutor":
            return AIMessage(
                content="What does the *shape* of the distribution tell you "
                        "that a single number wouldn't?"
            )
        raise KeyError(f"unexpected role: {role!r}")


def _load_attempt(engine, attempt_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, user_id, question_id, started_at, completed_at, "
                "       status, final_score, proof_token_id "
                "FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).fetchone()


def _load_question(engine, question_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, assignment_id, category_id, qid, qtype, "
                "       prompt_md, rubric_md, max_attempts "
                "FROM questions WHERE id = :id"
            ),
            {"id": question_id},
        ).fetchone()


def _row_to_dict(row):
    return {k: getattr(row, k) for k in row._fields}


# ---------------------------------------------------------------------------
# Direct LangGraph tests
# ---------------------------------------------------------------------------

def test_full_loop_incorrect_partial_correct(engine, seeded):
    fake = ScriptedRouter(
        [
            GradeVerdict(
                verdict="incorrect", score=0.0,
                rationale="No mention of distribution shape.",
                weakest_concept="distribution shape",
            ),
            GradeVerdict(
                verdict="partial", score=0.5,
                rationale="Mentions shape; misses outliers.",
                weakest_concept="outliers",
            ),
            GradeVerdict(
                verdict="correct", score=1.0,
                rationale="Covers shape and outliers.",
                weakest_concept=None,
            ),
        ]
    )

    attempt = _row_to_dict(_load_attempt(engine, seeded["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seeded["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seeded["user_id"])

    submissions_text = [
        "I would compute the mean and call it a day.",
        "A histogram shows the shape of the distribution.",
        "A histogram shows the shape AND the outliers — both are hidden by a single mean.",
    ]

    for i, txt in enumerate(submissions_text, start=1):
        attempt = _row_to_dict(_load_attempt(engine, seeded["attempt_id"]))
        state = GraderState(
            attempt=attempt,
            question=question,
            submission_payload={"kind": "text", "text": txt},
            turn_index=i,
        )
        final = run(state)
        assert final.submission_id is not None
        if i < 3:
            assert final.status_after == "in_progress"
            assert final.tutor_reply, "tutor must reply on non-correct verdicts"
        else:
            assert final.status_after == "passed"
            assert final.tutor_reply is None, (
                "tutor node must be skipped on verdict='correct'"
            )

    # All three submissions persisted with the right verdicts.
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT turn_index, payload_kind, grader_verdict, tutor_reply_md "
                "FROM submissions WHERE attempt_id=:a ORDER BY turn_index"
            ),
            {"a": seeded["attempt_id"]},
        ).fetchall()
    assert [r.turn_index for r in rows] == [1, 2, 3]
    assert [r.grader_verdict for r in rows] == ["incorrect", "partial", "correct"]
    assert [r.payload_kind for r in rows] == ["text", "text", "text"]
    # First two have a tutor reply; the correct one does not.
    assert rows[0].tutor_reply_md and rows[1].tutor_reply_md
    assert rows[2].tutor_reply_md is None

    # Attempt closed as passed, history row appended.
    a = _load_attempt(engine, seeded["attempt_id"])
    assert a.status == "passed"
    assert a.completed_at is not None
    with engine.connect() as conn:
        hist = conn.execute(
            text(
                "SELECT user_id, assignment_id, category_id, question_id, attempt_id "
                "FROM user_question_history WHERE attempt_id=:a"
            ),
            {"a": seeded["attempt_id"]},
        ).fetchall()
    assert len(hist) == 1
    assert hist[0].user_id == seeded["user_id"]
    assert hist[0].question_id == seeded["question_id"]


def test_tutor_skipped_on_correct_first_try(engine, seeded):
    fake = ScriptedRouter(
        [
            GradeVerdict(
                verdict="correct", score=1.0,
                rationale="Nailed it on the first attempt.",
                weakest_concept=None,
            ),
        ]
    )

    attempt = _row_to_dict(_load_attempt(engine, seeded["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seeded["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seeded["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={"kind": "text", "text": "All the right words."},
        turn_index=1,
    )
    final = run(state)
    assert final.status_after == "passed"
    assert final.tutor_reply is None

    # Confirm the tutor role was never invoked on the router.
    assert all(call[0] != "tutor" for call in fake.calls)


def test_attempt_exhausted_after_max_attempts(engine, seeded):
    # max_attempts on the seeded question is 3 — three wrong answers should
    # close the attempt as 'exhausted'.
    fake = ScriptedRouter(
        [
            GradeVerdict(verdict="incorrect", score=0.0, rationale="r1"),
            GradeVerdict(verdict="incorrect", score=0.0, rationale="r2"),
            GradeVerdict(verdict="incorrect", score=0.0, rationale="r3"),
        ]
    )

    question = _row_to_dict(_load_question(engine, seeded["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seeded["user_id"])

    for i in range(1, 4):
        attempt = _row_to_dict(_load_attempt(engine, seeded["attempt_id"]))
        state = GraderState(
            attempt=attempt,
            question=question,
            submission_payload={"kind": "text", "text": f"wrong {i}"},
            turn_index=i,
        )
        final = run(state)
        if i < 3:
            assert final.status_after == "in_progress"
        else:
            assert final.status_after == "exhausted"

    a = _load_attempt(engine, seeded["attempt_id"])
    assert a.status == "exhausted"
    assert a.completed_at is not None

    with engine.connect() as conn:
        hist_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM user_question_history WHERE attempt_id=:a"
            ),
            {"a": seeded["attempt_id"]},
        ).scalar()
    assert hist_count == 0  # exhausted attempts do NOT append to history


# ---------------------------------------------------------------------------
# End-to-end HTTP flow through FastAPI
# ---------------------------------------------------------------------------

def _extract_csrf(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "csrf token not found in rendered HTML"
    return m.group(1)


@pytest.fixture
def client_with_router(engine, monkeypatch):
    LocalAuthBackend(engine).create_user("alice", "pw-12345", role="student")

    fake = ScriptedRouter(
        [
            GradeVerdict(
                verdict="incorrect", score=0.0,
                rationale="Missing distribution shape.",
                weakest_concept="distribution shape",
            ),
            GradeVerdict(
                verdict="correct", score=1.0,
                rationale="Mentions shape and outliers.",
                weakest_concept=None,
            ),
        ]
    )
    monkeypatch.setattr(student_routes, "_router_singleton", fake)
    monkeypatch.setattr(student_routes, "get_router", lambda: fake)

    with engine.begin() as conn:
        # need a real assignment + question for the picker
        assignment_id = conn.execute(
            text(
                "INSERT INTO assignments "
                "(slug, week, title, source_path, frontmatter_json, body_md, "
                " content_hash, max_credit_questions, loaded_at) "
                "VALUES ('e2e', 1, 'E2E', 'x', '{}', '', 'h2', 1, :c)"
            ),
            {"c": datetime.now(timezone.utc).isoformat()},
        ).lastrowid
        cat_id = conn.execute(
            text(
                "INSERT INTO categories (assignment_id, name, ordering_index) "
                "VALUES (:a, 'k', 0)"
            ),
            {"a": assignment_id},
        ).lastrowid
        conn.execute(
            text(
                "INSERT INTO questions "
                "(assignment_id, category_id, qid, qtype, prompt_md, rubric_md, "
                " max_attempts) "
                "VALUES (:a, :c, 'qX', 'text', 'p', 'r', 5)"
            ),
            {"a": assignment_id, "c": cat_id},
        )

    client = TestClient(app)
    return client, fake


def _login(client, username="alice", password="pw-12345"):
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_e2e_start_view_submit_correct(client_with_router):
    client, _ = client_with_router
    _login(client)

    sid = client.cookies.get("tutor_session")
    csrf = csrf_token_for(sid)

    # Start the assignment — server picks a question, creates attempt, redirects.
    r = client.post(
        "/assignments/e2e/start",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/attempts/")
    attempt_id = int(location.rsplit("/", 1)[1])

    # GET the attempt page — sees question prompt and an empty transcript.
    r = client.get(location)
    assert r.status_code == 200
    assert "Your answer" in r.text
    assert "qX" in r.text

    # Submit a wrong answer → HTMX swap response, attempt still in_progress.
    r = client.post(
        f"/attempts/{attempt_id}/submit",
        data={"student_text": "Just compute the mean."},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert "incorrect" in r.text.lower()
    assert "Submit" in r.text  # form is back

    # Submit a correct answer → attempt closes; response shows the closing msg.
    r = client.post(
        f"/attempts/{attempt_id}/submit",
        data={"student_text": "A histogram shows shape and outliers."},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    assert "Correct" in r.text or "passed" in r.text.lower()

    # Database state: 2 submissions, attempt passed, history row.
    from app.db import get_engine
    engine = get_engine()
    with engine.connect() as conn:
        verdicts = [
            r[0] for r in conn.execute(
                text(
                    "SELECT grader_verdict FROM submissions "
                    "WHERE attempt_id=:a ORDER BY turn_index"
                ),
                {"a": attempt_id},
            ).fetchall()
        ]
        attempt_row = conn.execute(
            text("SELECT status FROM attempts WHERE id=:a"),
            {"a": attempt_id},
        ).fetchone()
        hist_count = conn.execute(
            text("SELECT COUNT(*) FROM user_question_history WHERE attempt_id=:a"),
            {"a": attempt_id},
        ).scalar()
    assert verdicts == ["incorrect", "correct"]
    assert attempt_row.status == "passed"
    assert hist_count == 1


def test_e2e_submit_rejects_other_users_attempt(client_with_router, engine):
    client, _ = client_with_router
    _login(client)
    sid = client.cookies.get("tutor_session")
    csrf = csrf_token_for(sid)

    # Create a second user and an attempt belonging to *them*.
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        other_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('bob', 'x', 'student', :c)"
            ),
            {"c": now},
        ).lastrowid
        question_row = conn.execute(
            text("SELECT id FROM questions LIMIT 1")
        ).fetchone()
        other_attempt = conn.execute(
            text(
                "INSERT INTO attempts (user_id, question_id, started_at, status) "
                "VALUES (:u, :q, :s, 'in_progress')"
            ),
            {"u": other_id, "q": question_row.id, "s": now},
        ).lastrowid

    r = client.post(
        f"/attempts/{other_attempt}/submit",
        data={"student_text": "anything"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404
