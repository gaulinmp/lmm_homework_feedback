from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.auth import (
    SESSION_COOKIE_NAME,
    User,
    csrf_token_for,
    require_login,
)
from app.db import get_engine
from app.llm.grader import GraderState, build_grader_graph
from app.llm.router import LLMRouter
from app.picker import pick_next_question
from app.proof import _b64url_encode, _canonical_json
from app.templating import templates
from app.uploads import UploadError, validate_and_store

router = APIRouter()


_router_singleton: Optional[LLMRouter] = None


def get_router() -> LLMRouter:
    """Lazy LLMRouter accessor — tests monkeypatch this for a fake router."""
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = LLMRouter()
    return _router_singleton


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return {k: getattr(row, k) for k in row._fields}


def _load_attempt_for_user(
    engine: Engine, attempt_id: int, user_id: int
) -> tuple[dict, dict]:
    """Return (attempt_dict, question_dict). Raises 404 if missing/foreign."""
    with engine.connect() as conn:
        attempt = conn.execute(
            text(
                "SELECT id, user_id, question_id, started_at, completed_at, "
                "       status, final_score, proof_token_id "
                "FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).fetchone()
        if attempt is None or attempt.user_id != user_id:
            raise HTTPException(status_code=404, detail="attempt not found")
        question = conn.execute(
            text(
                "SELECT id, assignment_id, category_id, qid, qtype, "
                "       prompt_md, rubric_md, max_attempts "
                "FROM questions WHERE id = :id"
            ),
            {"id": attempt.question_id},
        ).fetchone()
    return _row_to_dict(attempt), _row_to_dict(question)


def _load_submissions(engine: Engine, attempt_id: int) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, turn_index, submitted_at, payload_kind, "
                "       payload_text, grader_verdict, grader_score, "
                "       grader_rationale, tutor_reply_md "
                "FROM submissions "
                "WHERE attempt_id = :a "
                "ORDER BY turn_index ASC"
            ),
            {"a": attempt_id},
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _attempt_context(
    engine: Engine,
    attempt: dict,
    question: dict,
    submissions: list[dict],
    csrf_token: str,
) -> dict:
    max_attempts = int(question.get("max_attempts") or 6)
    used = len(submissions)
    return {
        "attempt": attempt,
        "question": question,
        "submissions": submissions,
        "max_attempts": max_attempts,
        "attempts_remaining": max(max_attempts - used, 0),
        "csrf_token": csrf_token,
    }


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
    engine = get_engine()
    with engine.connect() as conn:
        a = conn.execute(
            text("SELECT id FROM assignments WHERE slug = :s"),
            {"s": slug},
        ).fetchone()
    if a is None:
        raise HTTPException(status_code=404, detail="assignment not found")

    try:
        question = pick_next_question(engine, user.id, a.id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    now = _now_iso()
    with engine.begin() as conn:
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempts "
                "(user_id, question_id, started_at, status) "
                "VALUES (:u, :q, :s, 'in_progress')"
            ),
            {"u": user.id, "q": question.id, "s": now},
        ).lastrowid
    return RedirectResponse(f"/attempts/{attempt_id}", status_code=303)


@router.get("/attempts/{attempt_id}")
def view_attempt(
    attempt_id: int,
    request: Request,
    user: User = Depends(require_login),
) -> Response:
    engine = get_engine()
    attempt, question = _load_attempt_for_user(engine, attempt_id, user.id)
    submissions = _load_submissions(engine, attempt_id)
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    ctx = _attempt_context(engine, attempt, question, submissions, csrf_token_for(sid))
    ctx["user"] = user
    return templates.TemplateResponse(request, "attempt.html", ctx)


@router.post("/attempts/{attempt_id}/submit")
async def submit_attempt(
    attempt_id: int,
    request: Request,
    user: User = Depends(require_login),
) -> Response:
    engine = get_engine()
    attempt, question = _load_attempt_for_user(engine, attempt_id, user.id)

    if attempt["status"] != "in_progress":
        raise HTTPException(status_code=409, detail="attempt is closed")
    qtype = question["qtype"]
    if qtype not in {"text", "image", "python", "excel"}:
        raise HTTPException(
            status_code=501, detail=f"qtype {qtype!r} not yet supported"
        )

    form = await request.form()
    submission_payload: dict = {}

    if qtype == "text":
        student_text = (form.get("student_text") or "").strip()
        if not student_text:
            raise HTTPException(status_code=400, detail="empty submission")
        submission_payload = {"kind": "text", "text": student_text}
    else:
        upload = form.get("submission_file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="missing uploaded file")
        data = await upload.read()
        try:
            stored = validate_and_store(
                qtype, getattr(upload, "filename", None), data
            )
        except UploadError as e:
            raise HTTPException(status_code=400, detail=str(e))
        submission_payload = {
            "kind": qtype,
            "artifact_path": str(stored.path),
            "text": stored.text,
        }

    prior = _load_submissions(engine, attempt_id)
    turn_index = len(prior) + 1

    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload=submission_payload,
        turn_index=turn_index,
    )
    run = build_grader_graph(get_router(), engine, user_id=user.id)
    final = run(state)

    with engine.connect() as conn:
        new_row = conn.execute(
            text(
                "SELECT id, turn_index, submitted_at, payload_kind, "
                "       payload_text, grader_verdict, grader_score, "
                "       grader_rationale, tutor_reply_md "
                "FROM submissions WHERE id = :id"
            ),
            {"id": final.submission_id},
        ).fetchone()
        attempt_after = conn.execute(
            text(
                "SELECT id, user_id, question_id, started_at, completed_at, "
                "       status, final_score, proof_token_id "
                "FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).fetchone()
    new_submission = _row_to_dict(new_row) if new_row else None
    attempt_after_dict = _row_to_dict(attempt_after)

    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    submissions_all = prior + ([new_submission] if new_submission else [])
    ctx = _attempt_context(
        engine, attempt_after_dict, question, submissions_all, csrf_token_for(sid)
    )
    ctx["user"] = user
    ctx["new_turn"] = new_submission

    return templates.TemplateResponse(request, "_turn.html", ctx)


@router.get("/tokens/{token_id}/receipt")
def token_receipt(
    token_id: int,
    request: Request,
    user: User = Depends(require_login),
) -> Response:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT pt.id, pt.attempt_id, pt.payload_json, pt.hmac_sig, "
                "       pt.issued_at, a.user_id "
                "FROM proof_tokens pt JOIN attempts a ON a.id = pt.attempt_id "
                "WHERE pt.id = :i"
            ),
            {"i": token_id},
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="token not found")
    if row.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=404, detail="token not found")

    payload = json.loads(row.payload_json)
    payload_b64 = _b64url_encode(_canonical_json(payload))
    token_str = f"{payload_b64}.{row.hmac_sig}"
    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    return templates.TemplateResponse(
        request,
        "receipt.html",
        {
            "user": user,
            "csrf_token": csrf_token_for(sid),
            "token": token_str,
            "payload": payload,
            "payload_pretty": json.dumps(payload, indent=2, sort_keys=True),
            "issued_at": row.issued_at,
        },
    )


__all__ = ["router", "get_router"]
