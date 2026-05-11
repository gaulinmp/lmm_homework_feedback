"""Phase 6 — proof token mint/verify, audit/receipt smoke tests."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.auth import LocalAuthBackend, auth_rate_limit, csrf_token_for
from app.llm.verdicts import GradeVerdict
from app.main import app
from app.proof import _b64url_decode, _b64url_encode, _canonical_json, mint, verify
from app.routes import student as student_routes


# ---------------------------------------------------------------------------
# Fixtures (shared shape with test_grading_loop)
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
def passed_attempt(engine):
    """Insert a user/assignment/question/attempt and a single 'correct' submission.

    Mirrors what the grader's persist node would have written when verdict=correct
    minus the actual mint() call, so tests can drive mint() in isolation.
    """
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
                "VALUES ('week3_visualization', 3, 'Viz', 'x', '{}', '', "
                "        'h', 1, :c)"
            ),
            {"c": now},
        ).lastrowid
        cat_id = conn.execute(
            text(
                "INSERT INTO categories (assignment_id, name, ordering_index) "
                "VALUES (:a, 'histogram', 0)"
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
                "(user_id, question_id, started_at, completed_at, status, "
                " final_score) "
                "VALUES (:u, :q, :s, :c, 'passed', 0.95)"
            ),
            {"u": user_id, "q": question_id, "s": now, "c": now},
        ).lastrowid
        conn.execute(
            text(
                "INSERT INTO submissions "
                "(attempt_id, turn_index, submitted_at, payload_kind, "
                " payload_text, grader_verdict, grader_score, grader_rationale) "
                "VALUES (:a, 1, :s, 'text', 'a histogram shows shape and outliers', "
                "        'correct', 0.95, 'good')"
            ),
            {"a": attempt_id, "s": now},
        )
    return {
        "user_id": user_id,
        "assignment_id": assignment_id,
        "category_id": cat_id,
        "question_id": question_id,
        "attempt_id": attempt_id,
    }


# ---------------------------------------------------------------------------
# mint / verify round-trip
# ---------------------------------------------------------------------------

def test_mint_inserts_row_and_stamps_attempt(engine, passed_attempt):
    pt = mint(passed_attempt["attempt_id"], engine=engine)
    assert pt.token.count(".") == 1
    assert pt.proof_token_id

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, attempt_id, payload_json, hmac_sig, issued_at, "
                "       canvas_posted_at FROM proof_tokens WHERE id = :i"
            ),
            {"i": pt.proof_token_id},
        ).fetchone()
        attempt = conn.execute(
            text("SELECT proof_token_id FROM attempts WHERE id = :a"),
            {"a": passed_attempt["attempt_id"]},
        ).fetchone()

    assert row.attempt_id == passed_attempt["attempt_id"]
    assert row.canvas_posted_at is None
    assert attempt.proof_token_id == pt.proof_token_id

    # Payload contains the §15 fields with correct values
    payload = json.loads(row.payload_json)
    for key in (
        "user_id",
        "username",
        "assignment_slug",
        "qid",
        "category",
        "attempt_id",
        "completed_at",
        "submission_count",
        "final_score",
        "answer_hash",
    ):
        assert key in payload, f"missing key {key}"
    assert payload["username"] == "alice"
    assert payload["assignment_slug"] == "week3_visualization"
    assert payload["qid"] == "q1"
    assert payload["category"] == "histogram"
    assert payload["submission_count"] == 1
    assert payload["final_score"] == pytest.approx(0.95)
    assert payload["answer_hash"].startswith("sha256:")


def test_mint_verify_round_trip(engine, passed_attempt):
    pt = mint(passed_attempt["attempt_id"], engine=engine)
    ok, payload = verify(pt.token)
    assert ok is True
    assert payload == pt.payload


def test_mint_is_idempotent_per_attempt(engine, passed_attempt):
    pt1 = mint(passed_attempt["attempt_id"], engine=engine)
    pt2 = mint(passed_attempt["attempt_id"], engine=engine)
    assert pt1.proof_token_id == pt2.proof_token_id
    assert pt1.token == pt2.token


def test_tampered_payload_fails(engine, passed_attempt):
    pt = mint(passed_attempt["attempt_id"], engine=engine)
    payload = dict(pt.payload)
    payload["final_score"] = 1.0  # tamper
    bad_payload_b64 = _b64url_encode(_canonical_json(payload))
    sig = pt.token.split(".", 1)[1]
    bad_token = f"{bad_payload_b64}.{sig}"
    ok, _ = verify(bad_token)
    assert ok is False


def test_tampered_signature_fails(engine, passed_attempt):
    pt = mint(passed_attempt["attempt_id"], engine=engine)
    payload_b64, sig_b64 = pt.token.split(".", 1)
    sig_bytes = bytearray(_b64url_decode(sig_b64))
    sig_bytes[0] ^= 0x01  # flip a bit
    bad_token = f"{payload_b64}.{_b64url_encode(bytes(sig_bytes))}"
    ok, _ = verify(bad_token)
    assert ok is False


def test_verify_handles_malformed_input():
    ok, payload = verify("")
    assert ok is False and payload == {}
    ok, payload = verify("not-a-token")
    assert ok is False and payload == {}
    ok, payload = verify("only.one.dot.too.many")
    assert ok is False and payload == {}
    # valid base64url but not JSON
    ok, payload = verify(f"{_b64url_encode(b'not-json')}.{_b64url_encode(b'sig')}")
    assert ok is False
    assert payload == {}


# ---------------------------------------------------------------------------
# Audit dump — markdown, missing-key cases
# ---------------------------------------------------------------------------

def test_audit_cli_handles_attempt_with_no_messages(engine, passed_attempt, capsys, monkeypatch):
    # Audit script imports get_engine; monkeypatched engine in fixture handles it.
    monkeypatch.setattr("app.db._engine", engine)
    from cli import audit as audit_mod

    monkeypatch.setattr(audit_mod, "init_db", lambda: None)
    monkeypatch.setattr(audit_mod, "get_engine", lambda: engine)

    rc = audit_mod.main(["--attempt", str(passed_attempt["attempt_id"])])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# Audit — attempt" in out
    assert "no LLM messages recorded" in out


def test_audit_cli_user_with_no_attempts(engine, capsys, monkeypatch):
    # User exists but has no attempts.
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('ghost', 'x', 'student', :c)"
            ),
            {"c": now},
        )
    from cli import audit as audit_mod

    monkeypatch.setattr(audit_mod, "init_db", lambda: None)
    monkeypatch.setattr(audit_mod, "get_engine", lambda: engine)

    rc = audit_mod.main(["--user", "ghost"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no attempts found" in out


def test_audit_cli_dumps_messages(engine, passed_attempt, capsys, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        sub_id = conn.execute(
            text("SELECT id FROM submissions WHERE attempt_id = :a"),
            {"a": passed_attempt["attempt_id"]},
        ).fetchone().id
        conn.execute(
            text(
                "INSERT INTO llm_messages "
                "(attempt_id, submission_id, role_bucket, provider, model, "
                " role, content, latency_ms, created_at) "
                "VALUES (:a, :s, 'grader', 'openai_compat', 'qwen3', "
                "        'user', 'You are a strict grader.', 42, :c)"
            ),
            {"a": passed_attempt["attempt_id"], "s": sub_id, "c": now},
        )

    from cli import audit as audit_mod
    monkeypatch.setattr(audit_mod, "init_db", lambda: None)
    monkeypatch.setattr(audit_mod, "get_engine", lambda: engine)

    rc = audit_mod.main(["--attempt", str(passed_attempt["attempt_id"])])
    out = capsys.readouterr().out
    assert rc == 0
    assert "## Attempt" in out
    assert "openai_compat" in out
    assert "strict grader" in out
    assert "42 ms" in out


# ---------------------------------------------------------------------------
# Receipt route + admin audit views
# ---------------------------------------------------------------------------

def _login(client, username, password):
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303


@pytest.fixture
def web_client(engine, passed_attempt, monkeypatch):
    from app.auth import _ph

    real_hash = _ph.hash("pw-12345")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET password_hash = :p WHERE username = 'alice'"),
            {"p": real_hash},
        )
    LocalAuthBackend(engine).create_user("admin", "pw-12345", role="admin")

    client = TestClient(app)
    return client, passed_attempt


def test_receipt_route_renders_token(web_client, engine):
    client, attempt = web_client
    pt = mint(attempt["attempt_id"], engine=engine)
    _login(client, "alice", "pw-12345")
    r = client.get(f"/tokens/{pt.proof_token_id}/receipt")
    assert r.status_code == 200
    assert pt.token in r.text
    assert "week3_visualization" in r.text
    assert "Copy to clipboard" in r.text
    assert "Print" in r.text


def test_receipt_route_404_for_other_user(web_client, engine):
    client, attempt = web_client
    pt = mint(attempt["attempt_id"], engine=engine)
    # Create a third user, log in as them.
    LocalAuthBackend(engine).create_user("bob", "pw-12345", role="student")
    _login(client, "bob", "pw-12345")
    r = client.get(f"/tokens/{pt.proof_token_id}/receipt")
    assert r.status_code == 404


def test_admin_audit_attempt_view(web_client, engine):
    client, attempt = web_client
    mint(attempt["attempt_id"], engine=engine)
    _login(client, "admin", "pw-12345")
    r = client.get(f"/admin/audit/attempt/{attempt['attempt_id']}")
    assert r.status_code == 200
    assert "Audit — attempt" in r.text


def test_admin_audit_user_view(web_client, engine):
    client, attempt = web_client
    _login(client, "admin", "pw-12345")
    with engine.connect() as conn:
        uid = conn.execute(
            text(
                "SELECT user_id FROM attempts WHERE id = :a"
            ),
            {"a": attempt["attempt_id"]},
        ).fetchone().user_id
    r = client.get(f"/admin/audit/{uid}")
    assert r.status_code == 200
    assert "Audit — user" in r.text


def test_admin_audit_requires_admin(web_client):
    client, attempt = web_client
    _login(client, "alice", "pw-12345")
    r = client.get(f"/admin/audit/attempt/{attempt['attempt_id']}")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Integration with persist node
# ---------------------------------------------------------------------------

class ScriptedRouter:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)

    def invoke(self, role, messages, *, response_schema=None, files=None,
               attempt_id=None, submission_id=None):
        if role == "grader":
            v = self.verdicts.pop(0)
            return AIMessage(content="", additional_kwargs={"parsed": v})
        if role == "tutor":
            return AIMessage(content="hint")
        raise KeyError(role)


def test_grader_persist_mints_token_on_correct(engine):
    """End-to-end: a 'correct' verdict through the grader graph results in a
    proof_tokens row + attempts.proof_token_id stamped."""
    from app.llm.grader import GraderState, build_grader_graph

    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('eve', 'x', 'student', :c)"
            ),
            {"c": now},
        ).lastrowid
        assignment_id = conn.execute(
            text(
                "INSERT INTO assignments "
                "(slug, week, title, source_path, frontmatter_json, body_md, "
                " content_hash, max_credit_questions, loaded_at) "
                "VALUES ('end-to-end', 1, 'E2E', 'x', '{}', '', 'h', 1, :c)"
            ),
            {"c": now},
        ).lastrowid
        cat_id = conn.execute(
            text(
                "INSERT INTO categories (assignment_id, name, ordering_index) "
                "VALUES (:a, 'g', 0)"
            ),
            {"a": assignment_id},
        ).lastrowid
        question_id = conn.execute(
            text(
                "INSERT INTO questions "
                "(assignment_id, category_id, qid, qtype, prompt_md, rubric_md, "
                " max_attempts) "
                "VALUES (:a, :c, 'q1', 'text', 'p', 'r', 3)"
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

    fake = ScriptedRouter(
        [
            GradeVerdict(
                verdict="correct", score=1.0,
                rationale="Right.", weakest_concept=None,
            ),
        ]
    )
    with engine.connect() as conn:
        attempt = conn.execute(
            text(
                "SELECT id, user_id, question_id, started_at, completed_at, "
                "       status, final_score, proof_token_id "
                "FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).fetchone()
        question = conn.execute(
            text(
                "SELECT id, assignment_id, category_id, qid, qtype, "
                "       prompt_md, rubric_md, max_attempts "
                "FROM questions WHERE id = :id"
            ),
            {"id": question_id},
        ).fetchone()
    state = GraderState(
        attempt={k: getattr(attempt, k) for k in attempt._fields},
        question={k: getattr(question, k) for k in question._fields},
        submission_payload={"kind": "text", "text": "right answer"},
        turn_index=1,
    )
    run = build_grader_graph(fake, engine, user_id=user_id)
    final = run(state)
    assert final.status_after == "passed"

    with engine.connect() as conn:
        attempt_after = conn.execute(
            text("SELECT proof_token_id FROM attempts WHERE id = :a"),
            {"a": attempt_id},
        ).fetchone()
        token_row = conn.execute(
            text("SELECT id, payload_json FROM proof_tokens WHERE attempt_id = :a"),
            {"a": attempt_id},
        ).fetchone()
    assert attempt_after.proof_token_id is not None
    assert token_row is not None
    assert attempt_after.proof_token_id == token_row.id
    payload = json.loads(token_row.payload_json)
    assert payload["username"] == "eve"
    assert payload["qid"] == "q1"


# ---------------------------------------------------------------------------
# verify_token CLI smoke test (driven through the function, not subprocess)
# ---------------------------------------------------------------------------

def test_verify_token_cli_ok(engine, passed_attempt, capsys, monkeypatch):
    pt = mint(passed_attempt["attempt_id"], engine=engine)
    from cli import verify_token as vt
    rc = vt.main([pt.token])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("OK")
    assert "username" in out


def test_verify_token_cli_fail(capsys):
    from cli import verify_token as vt
    rc = vt.main(["payload.signature"])
    out = capsys.readouterr().out
    assert rc == 1
    assert out.startswith("FAIL")
