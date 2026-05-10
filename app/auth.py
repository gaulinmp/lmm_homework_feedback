from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol
from urllib.parse import parse_qs

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError
from fastapi import HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.db import get_engine

SESSION_COOKIE_NAME = "tutor_session"
SESSION_LIFETIME = timedelta(days=7)
CSRF_HEADER_NAME = "x-csrf-token"
CSRF_FORM_FIELD = "csrf_token"
DISABLED_PREFIX = "!"

_ph = PasswordHasher()


class LoginRequired(Exception):
    """Raised when an unauthenticated user hits a protected route."""


@dataclass
class User:
    id: int
    username: str
    role: str


class AuthBackend(Protocol):
    def authenticate(self, username: str, password: str) -> Optional[User]: ...

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "student",
        *,
        canvas_user_id: Optional[str] = None,
    ) -> User: ...


class LocalAuthBackend:
    def __init__(self, engine: Engine):
        self.engine = engine

    def authenticate(self, username: str, password: str) -> Optional[User]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, username, password_hash, role "
                    "FROM users WHERE username = :u"
                ),
                {"u": username},
            ).fetchone()
        if row is None:
            return None
        if row.password_hash.startswith(DISABLED_PREFIX):
            return None
        try:
            _ph.verify(row.password_hash, password)
        except (VerifyMismatchError, InvalidHash):
            return None

        if _ph.check_needs_rehash(row.password_hash):
            new_hash = _ph.hash(password)
            with self.engine.begin() as conn:
                conn.execute(
                    text("UPDATE users SET password_hash = :p WHERE id = :i"),
                    {"p": new_hash, "i": row.id},
                )
        return User(id=row.id, username=row.username, role=row.role)

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "student",
        *,
        canvas_user_id: Optional[str] = None,
    ) -> User:
        if role not in {"student", "admin"}:
            raise ValueError(f"invalid role: {role!r}")
        password_hash = _ph.hash(password)
        now = datetime.now(timezone.utc).isoformat()
        with self.engine.begin() as conn:
            uid = conn.execute(
                text(
                    "INSERT INTO users "
                    "(username, password_hash, role, created_at, canvas_user_id) "
                    "VALUES (:u, :p, :r, :c, :cu)"
                ),
                {
                    "u": username,
                    "p": password_hash,
                    "r": role,
                    "c": now,
                    "cu": canvas_user_id,
                },
            ).lastrowid
        return User(id=uid, username=username, role=role)


def create_session(engine: Engine, user_id: int) -> str:
    sid = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + SESSION_LIFETIME
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO sessions (id, user_id, created_at, expires_at) "
                "VALUES (:i, :u, :c, :e)"
            ),
            {
                "i": sid,
                "u": user_id,
                "c": now.isoformat(),
                "e": expires.isoformat(),
            },
        )
    return sid


def revoke_session(engine: Engine, sid: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sessions SET revoked_at = :n "
                "WHERE id = :i AND revoked_at IS NULL"
            ),
            {"n": now, "i": sid},
        )


def revoke_all_sessions(engine: Engine, user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sessions SET revoked_at = :n "
                "WHERE user_id = :u AND revoked_at IS NULL"
            ),
            {"n": now, "u": user_id},
        )


def _load_session_user(engine: Engine, sid: str) -> Optional[User]:
    if not sid:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT s.expires_at, s.revoked_at, "
                "u.id AS uid, u.username, u.role, u.password_hash "
                "FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.id = :i"
            ),
            {"i": sid},
        ).fetchone()
    if row is None or row.revoked_at:
        return None
    try:
        expires = datetime.fromisoformat(row.expires_at)
    except ValueError:
        return None
    if expires < datetime.now(timezone.utc):
        return None
    if row.password_hash.startswith(DISABLED_PREFIX):
        return None
    return User(id=row.uid, username=row.username, role=row.role)


def _is_secure() -> bool:
    return settings.ENV == "prod"


def set_session_cookie(response: Response, sid: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        sid,
        httponly=True,
        samesite="lax",
        secure=_is_secure(),
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def csrf_token_for(sid: str) -> str:
    if not sid:
        return ""
    return hmac.new(
        settings.SESSION_SECRET.encode("utf-8"),
        msg=sid.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def verify_csrf(sid: str, token: str) -> bool:
    if not sid or not token:
        return False
    return hmac.compare_digest(csrf_token_for(sid), token)


def current_user(request: Request) -> Optional[User]:
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    return _load_session_user(get_engine(), sid)


def require_login(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise LoginRequired()
    return user


def require_role(role: str) -> Callable[[Request], User]:
    def _dep(request: Request) -> User:
        user = current_user(request)
        if user is None:
            raise LoginRequired()
        if user.role != role:
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return _dep


class _LeakyBucket:
    def __init__(self, *, max_attempts: int, window_sec: float):
        self.max_attempts = max_attempts
        self.window_sec = window_sec
        self._state: dict[str, list[float]] = {}

    def _evict(self, bucket: list[float], now: float) -> None:
        cutoff = now - self.window_sec
        while bucket and bucket[0] < cutoff:
            bucket.pop(0)

    def allowed(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._state.setdefault(key, [])
        self._evict(bucket, now)
        return len(bucket) < self.max_attempts

    def record(self, key: str) -> None:
        now = time.monotonic()
        bucket = self._state.setdefault(key, [])
        self._evict(bucket, now)
        bucket.append(now)

    def reset(self) -> None:
        self._state.clear()


auth_rate_limit = _LeakyBucket(max_attempts=5, window_sec=60.0)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _cookie_from_scope(scope: Scope, name: str) -> str:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                part = part.strip()
                if "=" in part:
                    n, val = part.split("=", 1)
                    if n.strip() == name:
                        return val.strip()
    return ""


def _header_from_scope(scope: Scope, name: str) -> str:
    needle = name.lower().encode("latin-1")
    for k, v in scope.get("headers", []):
        if k.lower() == needle:
            return v.decode("latin-1")
    return ""


class CSRFMiddleware:
    """ASGI middleware enforcing a per-session CSRF token on non-GET requests.

    The token is the HMAC-SHA256 of the session id under SESSION_SECRET.
    For form posts, the token may come from a hidden ``csrf_token`` field; for
    HTMX/AJAX, from the ``X-CSRF-Token`` header.
    """

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, app: ASGIApp, exempt_paths: tuple[str, ...] = ()):
        self.app = app
        self.exempt_paths = tuple(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        if method in self.SAFE_METHODS or self._exempt(path):
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)
        sid = _cookie_from_scope(scope, SESSION_COOKIE_NAME)
        token = _header_from_scope(scope, CSRF_HEADER_NAME)
        if not token:
            content_type = _header_from_scope(scope, "content-type").lower()
            if "application/x-www-form-urlencoded" in content_type and body:
                parsed = parse_qs(body.decode("utf-8", errors="replace"))
                vals = parsed.get(CSRF_FORM_FIELD)
                if vals:
                    token = vals[0]

        if not verify_csrf(sid, token):
            await _send_plain(send, 403, "csrf token missing or invalid")
            return

        await self.app(scope, _replay_receive(body), send)

    def _exempt(self, path: str) -> bool:
        for p in self.exempt_paths:
            if path == p or path.startswith(p.rstrip("/") + "/"):
                return True
        return False


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    more = True
    while more:
        msg: Message = await receive()
        if msg["type"] != "http.request":
            continue
        chunks.append(msg.get("body", b"") or b"")
        more = msg.get("more_body", False)
    return b"".join(chunks)


def _replay_receive(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _send_plain(send: Send, status_code: int, body: str) -> None:
    payload = body.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload, "more_body": False})
