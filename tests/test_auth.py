from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.auth import (
    DISABLED_PREFIX,
    LocalAuthBackend,
    _ph,
    auth_rate_limit,
    create_session,
    csrf_token_for,
    revoke_all_sessions,
    verify_csrf,
)
from app.main import app


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
def client(engine) -> TestClient:  # noqa: ARG001 — fixture order matters
    return TestClient(app)


@pytest.fixture
def demo_user(engine):
    return LocalAuthBackend(engine).create_user(
        "demo", "secret-pw-123", role="student"
    )


@pytest.fixture
def demo_admin(engine):
    return LocalAuthBackend(engine).create_user(
        "boss", "admin-pw-456", role="admin"
    )


# ---------------------------------------------------------------------------
# Argon2 round-trip and password handling
# ---------------------------------------------------------------------------

def test_argon2_round_trip():
    h = _ph.hash("hunter2!")
    _ph.verify(h, "hunter2!")
    with pytest.raises(Exception):
        _ph.verify(h, "wrong")


def test_local_auth_backend_authenticate(engine, demo_user):  # noqa: ARG001
    backend = LocalAuthBackend(engine)
    user = backend.authenticate("demo", "secret-pw-123")
    assert user is not None
    assert user.username == "demo"
    assert user.role == "student"
    assert backend.authenticate("demo", "wrong-password") is None
    assert backend.authenticate("nobody", "secret-pw-123") is None


def test_disabled_user_cannot_authenticate(engine, demo_user):
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE users SET password_hash = :p WHERE id = :i"
            ),
            {
                "p": DISABLED_PREFIX
                + _ph.hash("secret-pw-123"),
                "i": demo_user.id,
            },
        )
    assert LocalAuthBackend(engine).authenticate("demo", "secret-pw-123") is None


# ---------------------------------------------------------------------------
# CSRF helpers
# ---------------------------------------------------------------------------

def test_csrf_helper_round_trip():
    sid = "deadbeef"
    tok = csrf_token_for(sid)
    assert tok and verify_csrf(sid, tok)
    assert not verify_csrf(sid, "wrong")
    assert not verify_csrf("", tok)


# ---------------------------------------------------------------------------
# Login/logout flow
# ---------------------------------------------------------------------------

def _extract_csrf_from_html(html: str) -> str:
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert m, "csrf token not found in rendered HTML"
    return m.group(1)


def test_unauth_root_redirects_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_get_renders_form(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "Log in" in r.text


def test_full_login_then_logout_flow(client, demo_user):  # noqa: ARG001
    r = client.post(
        "/login",
        data={"username": "demo", "password": "secret-pw-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "tutor_session" in r.cookies

    r = client.get("/")
    assert r.status_code == 200
    assert "demo" in r.text
    csrf = _extract_csrf_from_html(r.text)

    r = client.post(
        "/logout",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login"

    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_with_bad_password_returns_401(client, demo_user):  # noqa: ARG001
    r = client.post(
        "/login",
        data={"username": "demo", "password": "nope"},
        follow_redirects=False,
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# CSRF rejection
# ---------------------------------------------------------------------------

def test_logout_without_csrf_returns_403(client, demo_user):  # noqa: ARG001
    r = client.post(
        "/login",
        data={"username": "demo", "password": "secret-pw-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 403


def test_logout_with_bad_csrf_returns_403(client, demo_user):  # noqa: ARG001
    r = client.post(
        "/login",
        data={"username": "demo", "password": "secret-pw-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = client.post(
        "/logout",
        data={"csrf_token": "not-the-real-token"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_csrf_header_accepted_for_authenticated_post(client, demo_user):
    r = client.post(
        "/login",
        data={"username": "demo", "password": "secret-pw-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    sid = client.cookies.get("tutor_session")
    token = csrf_token_for(sid)
    r = client.post(
        "/logout",
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

def test_rate_limit_triggers_after_5_failed_attempts(client, demo_user):  # noqa: ARG001
    for _ in range(5):
        r = client.post(
            "/login",
            data={"username": "demo", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 401

    r = client.post(
        "/login",
        data={"username": "demo", "password": "wrong"},
        follow_redirects=False,
    )
    assert r.status_code == 429

    # Even a correct password is gated while the bucket is full.
    r = client.post(
        "/login",
        data={"username": "demo", "password": "secret-pw-123"},
        follow_redirects=False,
    )
    assert r.status_code == 429


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------

def test_admin_route_blocks_students(client, demo_user):  # noqa: ARG001
    r = client.post(
        "/login",
        data={"username": "demo", "password": "secret-pw-123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r = client.get("/admin/audit/1", follow_redirects=False)
    assert r.status_code == 403


def test_admin_route_allows_admin(client, demo_admin):  # noqa: ARG001
    r = client.post(
        "/login",
        data={"username": "boss", "password": "admin-pw-456"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Admin can hit the user-audit view; the admin's own user id is the only
    # one we know exists in this fixture.
    r = client.get(f"/admin/audit/{demo_admin.id}", follow_redirects=False)
    assert r.status_code == 200
    assert "Audit" in r.text


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def test_revoked_session_redirects_to_login(client, demo_user, engine):
    sid = create_session(engine, demo_user.id)
    client.cookies.set("tutor_session", sid)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200

    revoke_all_sessions(engine, demo_user.id)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_expired_session_redirects_to_login(client, demo_user, engine):
    sid = create_session(engine, demo_user.id)
    past = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE sessions SET expires_at = :e WHERE id = :i"),
            {"e": past, "i": sid},
        )
    client.cookies.set("tutor_session", sid)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
