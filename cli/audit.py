"""Dump the full LLM exchange for an attempt (or every attempt by a user).

Output is markdown — read by humans (the instructor when a student disputes a
grade), not by tools.

Usage::

    python cli/audit.py --attempt 1234
    python cli/audit.py --user demo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Optional

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app.db import get_engine, init_db  # noqa: E402


def _attempt_ids_for_user(engine: Engine, username: str) -> list[int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.id "
                "FROM attempts a JOIN users u ON u.id = a.user_id "
                "WHERE u.username = :u "
                "ORDER BY a.id ASC"
            ),
            {"u": username},
        ).fetchall()
    return [r.id for r in rows]


def _attempt_header(engine: Engine, attempt_id: int) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT a.id, a.status, a.final_score, a.started_at, "
                "       a.completed_at, a.proof_token_id, "
                "       u.username, q.qid, asg.slug AS assignment_slug, "
                "       cat.name AS category "
                "FROM attempts a "
                "JOIN users u       ON u.id = a.user_id "
                "JOIN questions q   ON q.id = a.question_id "
                "JOIN assignments asg ON asg.id = q.assignment_id "
                "JOIN categories cat  ON cat.id = q.category_id "
                "WHERE a.id = :a"
            ),
            {"a": attempt_id},
        ).fetchone()
    if row is None:
        return None
    return {k: getattr(row, k) for k in row._fields}


def _messages(engine: Engine, attempt_id: int) -> list:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT m.id, m.role_bucket, m.provider, m.model, m.role, "
                "       m.content, m.tool_name, m.tool_args_json, "
                "       m.tokens_in, m.tokens_out, m.latency_ms, "
                "       m.created_at, s.turn_index AS turn_index "
                "FROM llm_messages m "
                "LEFT JOIN submissions s ON s.id = m.submission_id "
                "WHERE m.attempt_id = :a "
                "ORDER BY m.id ASC"
            ),
            {"a": attempt_id},
        ).fetchall()
    return rows


def _emit_attempt(engine: Engine, attempt_id: int, out) -> None:
    header = _attempt_header(engine, attempt_id)
    if header is None:
        out.write(f"## Attempt {attempt_id} — *not found*\n\n")
        return
    out.write(f"## Attempt {header['id']} — {header['username']}\n\n")
    out.write(
        f"- assignment: `{header['assignment_slug']}`\n"
        f"- question: `{header['qid']}` (category: {header['category']})\n"
        f"- status: **{header['status']}** "
        f"(score: {header['final_score']})\n"
        f"- started: {header['started_at']}\n"
        f"- completed: {header['completed_at'] or '—'}\n"
        f"- proof_token_id: {header['proof_token_id'] or '—'}\n\n"
    )

    rows = _messages(engine, attempt_id)
    if not rows:
        out.write("_(no LLM messages recorded for this attempt)_\n\n")
        return

    for i, m in enumerate(rows, start=1):
        out.write(f"### Turn {m.turn_index or '—'} · message {i}\n\n")
        meta = (
            f"- bucket: `{m.role_bucket}` · role: `{m.role}` · "
            f"provider: `{m.provider}` · model: `{m.model}`"
        )
        if m.latency_ms is not None:
            meta += f" · latency: {m.latency_ms} ms"
        if m.tokens_in is not None or m.tokens_out is not None:
            meta += f" · tokens in/out: {m.tokens_in}/{m.tokens_out}"
        out.write(meta + "\n\n")
        if m.tool_name:
            out.write(
                f"- tool: `{m.tool_name}` args: `{m.tool_args_json}`\n\n"
            )
        out.write("```\n")
        out.write((m.content or "").rstrip() + "\n")
        out.write("```\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dump the full LLM exchange for an attempt or user."
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--attempt", type=int, help="attempt id")
    g.add_argument("--user", type=str, help="username")
    args = parser.parse_args(argv)

    init_db()
    engine = get_engine()

    if args.attempt is not None:
        attempt_ids: Iterable[int] = [args.attempt]
        title = f"Audit — attempt {args.attempt}"
    else:
        attempt_ids = _attempt_ids_for_user(engine, args.user)
        title = f"Audit — user `{args.user}`"

    out = sys.stdout
    out.write(f"# {title}\n\n")
    if not attempt_ids:
        out.write("_(no attempts found)_\n")
        return 0
    for aid in attempt_ids:
        _emit_attempt(engine, aid, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
