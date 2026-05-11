"""Phase 7 — SSE streaming endpoint.

The submit POST persists a guardrail-cleared tutor reply, then returns a
placeholder turn. The student's browser opens an SSE connection to
``GET /attempts/{id}/stream?submission=N`` and the saved reply is emitted
chunk-by-chunk as ``tutor-chunk`` events, terminated by a ``tutor-done`` event.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.auth import LocalAuthBackend, auth_rate_limit, csrf_token_for
from app.main import app
from app.routes import student as student_routes


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
    """Create a user + attempt + a completed submission with a tutor reply."""
    LocalAuthBackend(engine).create_user("alice", "pw-12345", role="student")
    now = datetime.now(timezone.utc).isoformat()
    tutor_reply = (
        "Think about which aggregation operation the rubric implies.\n"
        "What summary statistic captures the shape?"
    )
    with engine.begin() as conn:
        user_id = conn.execute(
            text("SELECT id FROM users WHERE username='alice'")
        ).scalar()
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
                "VALUES (:a, :c, 'q1', 'text', 'p', 'r', 6)"
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
        submission_id = conn.execute(
            text(
                "INSERT INTO submissions "
                "(attempt_id, turn_index, submitted_at, payload_kind, "
                " payload_text, grader_verdict, grader_score, grader_rationale, "
                " tutor_reply_md) "
                "VALUES (:a, 1, :s, 'text', 'wrong', 'partial', 0.5, "
                " 'missed something', :r)"
            ),
            {"a": attempt_id, "s": now, "r": tutor_reply},
        ).lastrowid
    return {
        "user_id": user_id,
        "attempt_id": attempt_id,
        "submission_id": submission_id,
        "tutor_reply": tutor_reply,
    }


def _login(client, username="alice", password="pw-12345"):
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303


@pytest.fixture
def client(engine, monkeypatch):
    # Speed up the inter-chunk delay so the test doesn't drag.
    monkeypatch.setattr(student_routes, "SSE_CHUNK_DELAY_SECONDS", 0.0)
    return TestClient(app)


def test_stream_emits_tutor_reply_chunks(client, seeded):
    _login(client)
    r = client.get(
        f"/attempts/{seeded['attempt_id']}/stream",
        params={"submission": seeded["submission_id"]},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    body = r.text
    # Should contain multiple tutor-chunk events
    chunk_events = re.findall(r"event: tutor-chunk", body)
    assert len(chunk_events) >= 5, f"expected multiple chunks, got {len(chunk_events)}"

    # And a terminating tutor-done event
    assert "event: tutor-done" in body

    # Reassemble the streamed chunks — they should equal the stored reply
    chunks: list[str] = []
    current_event: str | None = None
    current_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("event: "):
            current_event = line[len("event: "):]
            current_lines = []
        elif line.startswith("data: "):
            current_lines.append(line[len("data: "):])
        elif line == "":
            if current_event == "tutor-chunk":
                chunks.append("\n".join(current_lines))
            current_event = None
            current_lines = []

    reassembled = "".join(chunks)
    assert reassembled == seeded["tutor_reply"]


def test_stream_rejects_foreign_attempt(client, engine, seeded):
    """A second user must not be able to stream Alice's submission."""
    LocalAuthBackend(engine).create_user("bob", "pw-12345", role="student")
    _login(client, "bob")
    r = client.get(
        f"/attempts/{seeded['attempt_id']}/stream",
        params={"submission": seeded["submission_id"]},
    )
    assert r.status_code == 404


def test_stream_handles_missing_submission(client, seeded):
    _login(client)
    r = client.get(
        f"/attempts/{seeded['attempt_id']}/stream",
        params={"submission": 999_999},
    )
    assert r.status_code == 404


def test_stream_with_empty_reply_emits_done_only(client, engine, seeded):
    """A submission with no tutor reply (e.g. correct verdict) sends only `done`."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE submissions SET tutor_reply_md=NULL WHERE id=:i"),
            {"i": seeded["submission_id"]},
        )
    _login(client)
    r = client.get(
        f"/attempts/{seeded['attempt_id']}/stream",
        params={"submission": seeded["submission_id"]},
    )
    assert r.status_code == 200
    assert "event: tutor-chunk" not in r.text
    assert "event: tutor-done" in r.text
