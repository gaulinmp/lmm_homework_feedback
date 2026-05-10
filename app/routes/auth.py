from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.auth import (
    LocalAuthBackend,
    SESSION_COOKIE_NAME,
    auth_rate_limit,
    clear_session_cookie,
    client_ip,
    create_session,
    current_user,
    csrf_token_for,
    revoke_session,
    set_session_cookie,
)
from app.db import get_engine
from app.templating import templates

router = APIRouter()


def _render_login(
    request: Request, *, error: str | None = None, status_code: int = 200
) -> Response:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "csrf_token": "", "user": None},
        status_code=status_code,
    )


@router.get("/login")
def login_form(request: Request) -> Response:
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _render_login(request)


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    ip = client_ip(request)
    if not auth_rate_limit.allowed(ip):
        return _render_login(
            request,
            error="Too many failed attempts. Try again in a minute.",
            status_code=429,
        )

    backend = LocalAuthBackend(get_engine())
    user = backend.authenticate(username, password)
    if user is None:
        auth_rate_limit.record(ip)
        return _render_login(
            request, error="Invalid username or password.", status_code=401
        )

    sid = create_session(get_engine(), user.id)
    redirect = RedirectResponse("/", status_code=303)
    set_session_cookie(redirect, sid)
    return redirect


@router.post("/logout")
def logout(request: Request) -> Response:
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    if sid:
        revoke_session(get_engine(), sid)
    redirect = RedirectResponse("/login", status_code=303)
    clear_session_cookie(redirect)
    return redirect


__all__ = ["router", "csrf_token_for"]
