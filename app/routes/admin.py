from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.auth import (
    SESSION_COOKIE_NAME,
    User,
    csrf_token_for,
    require_role,
)
from app.db import get_engine
from app.templating import templates

router = APIRouter(prefix="/admin")


def _row_to_dict(row) -> dict[str, Any]:
    return {k: getattr(row, k) for k in row._fields}


def _load_messages_for_attempt(engine: Engine, attempt_id: int) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT m.id, m.attempt_id, m.submission_id, m.role_bucket, "
                "       m.provider, m.model, m.role, m.content, m.tool_name, "
                "       m.tool_args_json, m.tokens_in, m.tokens_out, "
                "       m.latency_ms, m.created_at, "
                "       s.turn_index AS turn_index "
                "FROM llm_messages m "
                "LEFT JOIN submissions s ON s.id = m.submission_id "
                "WHERE m.attempt_id = :a "
                "ORDER BY m.id ASC"
            ),
            {"a": attempt_id},
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _load_attempts_for_user(engine: Engine, user_id: int) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.id, a.user_id, a.question_id, a.started_at, "
                "       a.completed_at, a.status, a.final_score, "
                "       a.proof_token_id, q.qid, asg.slug AS assignment_slug "
                "FROM attempts a "
                "JOIN questions q   ON q.id = a.question_id "
                "JOIN assignments asg ON asg.id = q.assignment_id "
                "WHERE a.user_id = :u "
                "ORDER BY a.id DESC"
            ),
            {"u": user_id},
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _load_attempt(engine: Engine, attempt_id: int) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT a.id, a.user_id, a.question_id, a.started_at, "
                "       a.completed_at, a.status, a.final_score, "
                "       a.proof_token_id, u.username, q.qid, "
                "       asg.slug AS assignment_slug "
                "FROM attempts a "
                "JOIN users u       ON u.id = a.user_id "
                "JOIN questions q   ON q.id = a.question_id "
                "JOIN assignments asg ON asg.id = q.assignment_id "
                "WHERE a.id = :a"
            ),
            {"a": attempt_id},
        ).fetchone()
    return _row_to_dict(row) if row else None


def _load_user(engine: Engine, user_id: int) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, username, role FROM users WHERE id = :u"),
            {"u": user_id},
        ).fetchone()
    return _row_to_dict(row) if row else None


@router.get("/audit/attempt/{attempt_id}")
def audit_attempt(
    attempt_id: int,
    request: Request,
    user: User = Depends(require_role("admin")),
) -> Response:
    engine = get_engine()
    attempt = _load_attempt(engine, attempt_id)
    if attempt is None:
        raise HTTPException(status_code=404, detail="attempt not found")
    messages = _load_messages_for_attempt(engine, attempt_id)
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "user": user,
            "csrf_token": csrf_token_for(sid),
            "view": "attempt",
            "attempt": attempt,
            "messages": messages,
        },
    )


@router.get("/audit/{user_id}")
def audit_user(
    user_id: int,
    request: Request,
    user: User = Depends(require_role("admin")),
) -> Response:
    engine = get_engine()
    target = _load_user(engine, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    attempts = _load_attempts_for_user(engine, user_id)
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "user": user,
            "csrf_token": csrf_token_for(sid),
            "view": "user",
            "target_user": target,
            "attempts": attempts,
        },
    )
