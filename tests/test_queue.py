"""Phase 7 — per-role queues, per-user locks, and grader timeouts.

The router gates concurrent calls into each role bucket via an
``asyncio.Semaphore`` and tracks waiters for the queue-position UI. A
separate per-user ``asyncio.Lock`` prevents a single student from running
two grader submissions in parallel. The submit endpoint wraps the (sync)
grader graph in a thread + ``asyncio.wait_for`` so a stuck provider can be
surfaced as a polite error without consuming an attempt.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import threading
import time
from datetime import datetime, timezone

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
from app.routes import student as student_routes


# ---------------------------------------------------------------------------
# Direct tests on RoleBucket + user_lock
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_env(tmp_path, monkeypatch):
    db_file = tmp_path / "tutor.db"
    llm_cfg = tmp_path / "llm.toml"
    llm_cfg.write_text(
        '[roles.grader]\n'
        'provider = "openai_compat"\n'
        'base_url = "http://127.0.0.1:8080/v1"\n'
        'model = "qwen3"\n'
        '\n'
        '[roles.tutor]\n'
        'provider = "anthropic"\n'
        'model = "claude-haiku-4-5-20251001"\n'
        'api_key_env = "ANTHROPIC_API_KEY"\n'
        '\n'
        '[roles.cloud_grader]\n'
        'provider = "openai"\n'
        'model = "gpt-4o-mini"\n'
        'api_key_env = "OPENAI_API_KEY"\n'
    )
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("LLM_CONFIG_PATH", str(llm_cfg))

    from app import config as cm
    importlib.reload(cm)
    from app import db as dm
    importlib.reload(dm)
    dm.init_db()

    from app.llm import router as rm
    importlib.reload(rm)
    return rm


def test_local_grader_defaults_to_one_worker(fresh_env):
    rm = fresh_env
    r = rm.LLMRouter()
    bucket = r.role_bucket("grader")
    assert bucket.workers == 1


def test_cloud_role_defaults_to_unbounded_workers(fresh_env):
    rm = fresh_env
    r = rm.LLMRouter()
    assert r.role_bucket("tutor").workers >= 1024
    assert r.role_bucket("cloud_grader").workers >= 1024


def test_role_bucket_tracks_waiters(fresh_env):
    rm = fresh_env
    bucket = rm.RoleBucket(workers=1)

    async def runner():
        # Hold the slot, then start two more contenders, observe `waiting` rise.
        async def hold(barrier: asyncio.Event):
            async with bucket.acquire():
                await barrier.wait()

        release = asyncio.Event()
        first = asyncio.create_task(hold(release))
        # Let the first task acquire.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert bucket.in_flight == 1
        assert bucket.waiting == 0

        # Two more — they queue behind the first.
        second_release = asyncio.Event()
        second = asyncio.create_task(hold(second_release))
        third_release = asyncio.Event()
        third = asyncio.create_task(hold(third_release))
        # Yield enough times for both to register as waiters.
        for _ in range(5):
            await asyncio.sleep(0)
        assert bucket.waiting == 2

        # Release in order, asserting waiter count drops.
        release.set()
        await first
        for _ in range(5):
            await asyncio.sleep(0)
        assert bucket.waiting == 1
        second_release.set()
        await second
        third_release.set()
        await third
        assert bucket.waiting == 0

    asyncio.run(runner())


def test_user_lock_is_per_user_and_reused(fresh_env):
    rm = fresh_env
    r = rm.LLMRouter()
    lock_a = r.user_lock(1)
    lock_b = r.user_lock(1)
    lock_c = r.user_lock(2)
    assert lock_a is lock_b
    assert lock_a is not lock_c


def test_queue_status_snapshot(fresh_env):
    rm = fresh_env
    r = rm.LLMRouter()
    snap = r.queue_status("grader")
    assert snap["waiting"] == 0
    assert snap["in_flight"] == 0
    assert snap["workers"] == 1


# ---------------------------------------------------------------------------
# End-to-end: user lock + timeout via FastAPI
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
def seeded_question(engine):
    LocalAuthBackend(engine).create_user("alice", "pw-12345", role="student")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        user_id = conn.execute(
            text("SELECT id FROM users WHERE username='alice'")
        ).scalar()
        assignment_id = conn.execute(
            text(
                "INSERT INTO assignments "
                "(slug, week, title, source_path, frontmatter_json, body_md, "
                " content_hash, max_credit_questions, loaded_at) "
                "VALUES ('q', 1, 'Q', 'x', '{}', '', 'h', 1, :c)"
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
                "VALUES (:a, :c, 'qX', 'text', 'p', 'r', 5)"
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
        "question_id": question_id,
        "attempt_id": attempt_id,
    }


def _login(client, username="alice", password="pw-12345"):
    r = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert r.status_code == 303


class SlowRouter:
    """Fake router whose grader call blocks until ``release`` is set.

    Also exposes the same surface as the real LLMRouter (``user_lock``,
    ``role_bucket``, ``queue_status``) so the submit endpoint can rely on it.
    """

    def __init__(self, *, hold_seconds: float):
        self.hold_seconds = hold_seconds
        self._user_locks: dict[int, asyncio.Lock] = {}
        self.release_event = threading.Event()
        self.entered_grader = threading.Event()

    def invoke(self, role, messages, *, response_schema=None, files=None,
               attempt_id=None, submission_id=None):
        if role == "grader":
            self.entered_grader.set()
            # block for hold_seconds, or until release_event signals.
            self.release_event.wait(timeout=self.hold_seconds)
            return AIMessage(
                content="",
                additional_kwargs={
                    "parsed": GradeVerdict(
                        verdict="incorrect",
                        score=0.0,
                        rationale="nope",
                        weakest_concept="x",
                    )
                },
            )
        if role == "tutor":
            return AIMessage(content="What might you reconsider?")
        raise KeyError(role)

    def user_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def role_bucket(self, role: str):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def queue_status(self, role: str) -> dict[str, int]:
        return {"waiting": 0, "in_flight": 0, "workers": 1}


def test_timeout_surfaces_error_and_does_not_consume_attempt(
    engine, seeded_question, monkeypatch
):
    fake = SlowRouter(hold_seconds=5.0)
    monkeypatch.setattr(student_routes, "_router_singleton", fake)
    monkeypatch.setattr(student_routes, "get_router", lambda: fake)
    monkeypatch.setattr(student_routes, "TOTAL_TIMEOUT_SECONDS", 0.3)

    client = TestClient(app)
    _login(client)
    sid = client.cookies.get("tutor_session")
    csrf = csrf_token_for(sid)

    attempt_id = seeded_question["attempt_id"]
    r = client.post(
        f"/attempts/{attempt_id}/submit",
        data={"student_text": "anything"},
        headers={"X-CSRF-Token": csrf},
    )
    # Allow the held grader thread to exit so the test doesn't leak workers.
    fake.release_event.set()

    assert r.status_code == 200
    assert "timed out" in r.text.lower() or "timeout" in r.text.lower()

    # No submission row was written → attempt unconsumed.
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM submissions WHERE attempt_id=:a"),
            {"a": attempt_id},
        ).scalar()
        status = conn.execute(
            text("SELECT status FROM attempts WHERE id=:a"),
            {"a": attempt_id},
        ).scalar()
    assert count == 0
    assert status == "in_progress"


def test_per_user_lock_rejects_double_submit(
    engine, seeded_question, monkeypatch
):
    fake = SlowRouter(hold_seconds=2.0)
    monkeypatch.setattr(student_routes, "_router_singleton", fake)
    monkeypatch.setattr(student_routes, "get_router", lambda: fake)

    client = TestClient(app)
    _login(client)
    sid = client.cookies.get("tutor_session")
    csrf = csrf_token_for(sid)
    attempt_id = seeded_question["attempt_id"]

    # Kick off the first (slow) submit in a background thread. It will hold
    # the user lock until the grader returns.
    first_holder: dict[str, object] = {}

    def first_call():
        first_holder["resp"] = client.post(
            f"/attempts/{attempt_id}/submit",
            data={"student_text": "first"},
            headers={"X-CSRF-Token": csrf},
        )

    t = threading.Thread(target=first_call, daemon=True)
    t.start()

    # Wait until the slow grader has actually started (so the lock is taken).
    assert fake.entered_grader.wait(timeout=2.0)

    # Now fire a second submit — it should be rejected as a double-submit.
    r2 = client.post(
        f"/attempts/{attempt_id}/submit",
        data={"student_text": "second"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r2.status_code == 200
    assert "already" in r2.text.lower() or "wait" in r2.text.lower()

    # Allow the first call to finish.
    fake.release_event.set()
    t.join(timeout=5.0)
    assert first_holder.get("resp") is not None
    assert first_holder["resp"].status_code == 200

    # Exactly one submission row was written, from the first call.
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload_text FROM submissions WHERE attempt_id=:a "
                "ORDER BY id"
            ),
            {"a": attempt_id},
        ).fetchall()
    assert [r[0] for r in rows] == ["first"]


def test_queue_status_endpoint_renders_position(
    engine, seeded_question, monkeypatch
):
    """Queue-status endpoint returns the banner while a submit is in flight."""
    fake = SlowRouter(hold_seconds=2.0)
    monkeypatch.setattr(student_routes, "_router_singleton", fake)
    monkeypatch.setattr(student_routes, "get_router", lambda: fake)

    client = TestClient(app)
    _login(client)
    sid = client.cookies.get("tutor_session")
    csrf = csrf_token_for(sid)
    attempt_id = seeded_question["attempt_id"]

    # No in-flight submission → endpoint returns an empty banner area.
    r_idle = client.get(f"/attempts/{attempt_id}/queue-status")
    assert r_idle.status_code == 200
    assert "queue-banner" not in r_idle.text

    # Kick off slow submit, then poll the queue-status endpoint mid-flight.
    def first_call():
        client.post(
            f"/attempts/{attempt_id}/submit",
            data={"student_text": "first"},
            headers={"X-CSRF-Token": csrf},
        )

    t = threading.Thread(target=first_call, daemon=True)
    t.start()
    assert fake.entered_grader.wait(timeout=2.0)

    r_busy = client.get(f"/attempts/{attempt_id}/queue-status")
    assert r_busy.status_code == 200
    assert "queue-banner" in r_busy.text
    assert re.search(r"#\d+ in line", r_busy.text)

    fake.release_event.set()
    t.join(timeout=5.0)
