from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import text

from app.auth import (
    SESSION_COOKIE_NAME,
    User,
    csrf_token_for,
    require_login,
)
from app.db import get_engine
from app.templating import templates

router = APIRouter()


@router.get("/")
def picker(request: Request, user: User = Depends(require_login)) -> Response:
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT slug, week, title FROM assignments "
                "ORDER BY COALESCE(week, 9999), title"
            )
        ).fetchall()
    assignments = [
        {"slug": r.slug, "week": r.week, "title": r.title} for r in rows
    ]
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    return templates.TemplateResponse(
        request,
        "picker.html",
        {
            "user": user,
            "assignments": assignments,
            "csrf_token": csrf_token_for(sid),
        },
    )


@router.post("/assignments/{slug}/start")
def start_assignment(
    slug: str, user: User = Depends(require_login)
) -> Response:
    return HTMLResponse(
        f"<p>not yet implemented — slug={slug}</p>", status_code=501
    )
