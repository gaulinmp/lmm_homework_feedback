from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.auth import (
    SESSION_COOKIE_NAME,
    User,
    csrf_token_for,
    require_login,
)
from app.db import get_engine
from app.llm.grader import GraderState, build_grader_graph, mark_turn_cancelled
from app.llm.router import LLMRouter
from app.picker import pick_next_question
from app.proof import _b64url_encode, _canonical_json
from app.templating import templates
from app.uploads import UploadError, validate_and_store

logger = logging.getLogger(__name__)

router = APIRouter()


# Per-node timeouts come from §13 of the design doc. The grader graph is run
# end-to-end in a worker thread; we wrap the whole run with the sum since the
# nodes execute serially.
GRADE_TIMEOUT_SECONDS = 60
TUTOR_TIMEOUT_SECONDS = 120
TOTAL_TIMEOUT_SECONDS = GRADE_TIMEOUT_SECONDS + TUTOR_TIMEOUT_SECONDS

# SSE playback chunking. We chunk by word with a small inter-chunk delay so
# the student sees the tutor reply assemble token-by-token rather than as a
# single blob.
SSE_CHUNK_DELAY_SECONDS = 0.04


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
                "       prompt_md, rubric_md, reference_solution_md, "
                "       max_attempts "
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


def _render_timeout_error(
    request: Request,
    *,
    attempt: dict,
    question: dict,
    submissions: list[dict],
    csrf_token: str,
    user: User,
) -> Response:
    ctx = _attempt_context(get_engine(), attempt, question, submissions, csrf_token)
    ctx["user"] = user
    ctx["error_message"] = (
        "Grading timed out. Your attempt was not consumed — please try "
        "submitting again."
    )
    return templates.TemplateResponse(request, "_turn.html", ctx)


def _render_busy_error(
    request: Request,
    *,
    attempt: dict,
    question: dict,
    submissions: list[dict],
    csrf_token: str,
    user: User,
) -> Response:
    ctx = _attempt_context(get_engine(), attempt, question, submissions, csrf_token)
    ctx["user"] = user
    ctx["error_message"] = (
        "You already have a submission being graded. Please wait for the "
        "tutor to respond before submitting again."
    )
    return templates.TemplateResponse(request, "_turn.html", ctx)


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

    sid = request.cookies.get(SESSION_COOKIE_NAME, "")
    csrf = csrf_token_for(sid)

    llm_router = get_router()
    user_lock = (
        llm_router.user_lock(user.id)
        if hasattr(llm_router, "user_lock")
        else asyncio.Lock()
    )
    if user_lock.locked():
        prior = _load_submissions(engine, attempt_id)
        logger.info(
            "rejecting double-submit for user %s on attempt %s",
            user.id,
            attempt_id,
        )
        return _render_busy_error(
            request,
            attempt=attempt,
            question=question,
            submissions=prior,
            csrf_token=csrf,
            user=user,
        )

    async with user_lock:
        prior = _load_submissions(engine, attempt_id)
        turn_index = len(prior) + 1

        state = GraderState(
            attempt=attempt,
            question=question,
            submission_payload=submission_payload,
            turn_index=turn_index,
        )
        run = build_grader_graph(llm_router, engine, user_id=user.id)

        try:
            final = await asyncio.wait_for(
                asyncio.to_thread(run, state),
                timeout=TOTAL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            # Threads can't be cancelled; mark this turn so the orphan
            # grader's eventual persist() short-circuits and writes nothing.
            mark_turn_cancelled(attempt_id, turn_index)
            logger.error(
                "grader timeout on attempt %s for user %s (timeout=%ss)",
                attempt_id,
                user.id,
                TOTAL_TIMEOUT_SECONDS,
            )
            return _render_timeout_error(
                request,
                attempt=attempt,
                question=question,
                submissions=prior,
                csrf_token=csrf,
                user=user,
            )

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

    submissions_all = prior + ([new_submission] if new_submission else [])
    ctx = _attempt_context(
        engine, attempt_after_dict, question, submissions_all, csrf
    )
    ctx["user"] = user
    ctx["new_turn"] = new_submission

    return templates.TemplateResponse(request, "_turn.html", ctx)


@router.get("/attempts/{attempt_id}/queue-status")
def queue_status(
    attempt_id: int,
    request: Request,
    user: User = Depends(require_login),
) -> Response:
    """HTMX-polled fragment: 'you are #N in line for the tutor'.

    Renders nothing while no submission is in flight, otherwise a small
    banner naming the student's queue position behind the tutor role bucket.
    """
    engine = get_engine()
    attempt, _question = _load_attempt_for_user(engine, attempt_id, user.id)
    llm_router = get_router()
    user_lock_held = bool(
        hasattr(llm_router, "user_lock")
        and llm_router.user_lock(user.id).locked()
    )
    qstatus = getattr(llm_router, "queue_status", None)
    tutor_q = qstatus("tutor") if qstatus else {"waiting": 0, "in_flight": 0, "workers": 1}
    grader_q = qstatus("grader") if qstatus else {"waiting": 0, "in_flight": 0, "workers": 1}

    in_flight = bool(user_lock_held)
    waiting_ahead = tutor_q["waiting"] + grader_q["waiting"]
    position = waiting_ahead + 1 if in_flight else 0
    return templates.TemplateResponse(
        request,
        "_queue_status.html",
        {
            "attempt": attempt,
            "in_flight": in_flight,
            "position": position,
            "tutor_queue": tutor_q,
            "grader_queue": grader_q,
        },
    )


async def _stream_text_words(reply: str):
    """Split a tutor reply into small chunks and yield them with a tiny delay.

    We stream the *guardrail-cleared* reply that was already written to the
    submission row. That keeps leakage safety pre-checked; the SSE wire is
    purely a presentation layer that gives the student a live-typing feel.
    """
    if not reply:
        return
    buf: list[str] = []
    for char in reply:
        buf.append(char)
        if char == " " or char == "\n":
            chunk = "".join(buf)
            buf = []
            yield chunk
            await asyncio.sleep(SSE_CHUNK_DELAY_SECONDS)
    if buf:
        yield "".join(buf)


@router.get("/attempts/{attempt_id}/stream")
async def stream_attempt(
    attempt_id: int,
    submission: int,
    request: Request,
    user: User = Depends(require_login),
) -> Response:
    """SSE endpoint that streams a submission's tutor reply token-by-token.

    The submit POST persists the guardrail-cleared tutor reply, then returns
    a placeholder turn that opens this stream. HTMX's ``sse-swap`` consumes
    the events and assembles the text into the tutor reply slot.
    """
    engine = get_engine()
    # Verify ownership of the attempt before exposing the submission's reply.
    _attempt, _question = _load_attempt_for_user(engine, attempt_id, user.id)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, attempt_id, tutor_reply_md "
                "FROM submissions WHERE id = :id"
            ),
            {"id": submission},
        ).fetchone()
    if row is None or row.attempt_id != attempt_id:
        raise HTTPException(status_code=404, detail="submission not found")

    reply = row.tutor_reply_md or ""

    def _sse_event(event: str, data: str) -> bytes:
        # SSE wire format: each event is one or more `field: value\n` lines
        # terminated by a blank line. We escape internal newlines as separate
        # data lines so multi-line tutor replies render correctly.
        lines = [f"event: {event}"]
        for line in data.split("\n"):
            lines.append(f"data: {line}")
        return ("\n".join(lines) + "\n\n").encode("utf-8")

    async def event_gen():
        async for chunk in _stream_text_words(reply):
            if await request.is_disconnected():
                return
            yield _sse_event("tutor-chunk", chunk)
        yield _sse_event("tutor-done", "")

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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
