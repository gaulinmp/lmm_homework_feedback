from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.assignments_loader import load_assignment
from app.picker import pick_next_question


def _make_engine() -> Engine:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _fks(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    with engine.begin() as conn:
        for stmt in db_module.SCHEMA_STATEMENTS:
            conn.exec_driver_sql(stmt)
    return engine


_FIXTURE = """\
---
slug: pick_demo
week: 1
title: "Picker Demo"
max_credit_questions: 4
categories:
  - name: a
  - name: b
  - name: c
  - name: d
---

## Question q1
```yaml
qid: q1
category: a
type: text
rubric: r
```
prompt-a-1

## Question q2
```yaml
qid: q2
category: a
type: text
rubric: r
```
prompt-a-2

## Question q3
```yaml
qid: q3
category: b
type: text
rubric: r
```
prompt-b-1

## Question q4
```yaml
qid: q4
category: b
type: text
rubric: r
```
prompt-b-2

## Question q5
```yaml
qid: q5
category: c
type: text
rubric: r
```
prompt-c-1

## Question q6
```yaml
qid: q6
category: c
type: text
rubric: r
```
prompt-c-2

## Question q7
```yaml
qid: q7
category: d
type: text
rubric: r
```
prompt-d-1

## Question q8
```yaml
qid: q8
category: d
type: text
rubric: r
```
prompt-d-2
"""


@pytest.fixture
def loaded(tmp_path: Path) -> tuple[Engine, int, int]:
    """Engine + assignment_id + user_id, with the demo assignment loaded."""
    engine = _make_engine()
    path = tmp_path / "pick_demo.md"
    path.write_text(_FIXTURE, encoding="utf-8")
    load_assignment(engine, path)

    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        assignment_id = conn.execute(text("SELECT id FROM assignments")).scalar()
        user_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (:u, :p, 'student', :c)"
            ),
            {"u": "alice", "p": "x", "c": now},
        ).lastrowid
    return engine, assignment_id, user_id


def _record_history(engine: Engine, user_id: int, assignment_id: int, question) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempts (user_id, question_id, started_at, completed_at, status, final_score) "
                "VALUES (:u, :q, :s, :c, 'passed', 1.0)"
            ),
            {"u": user_id, "q": question.id, "s": now, "c": now},
        ).lastrowid
        conn.execute(
            text(
                "INSERT INTO user_question_history "
                "(user_id, assignment_id, category_id, question_id, attempt_id, completed_at) "
                "VALUES (:u, :a, :cat, :q, :att, :ts)"
            ),
            {
                "u": user_id, "a": assignment_id, "cat": question.category_id,
                "q": question.id, "att": attempt_id, "ts": now,
            },
        )


def test_picker_visits_each_category_before_repeating(loaded):
    engine, assignment_id, user_id = loaded
    rng = random.Random(42)

    seen_categories: list[int] = []
    for _ in range(4):
        q = pick_next_question(engine, user_id, assignment_id, rng=rng)
        assert q.category_id not in seen_categories, (
            f"category {q.category_id} repeated; seen={seen_categories}"
        )
        seen_categories.append(q.category_id)
        _record_history(engine, user_id, assignment_id, q)

    assert len(set(seen_categories)) == 4


def test_picker_falls_back_when_all_categories_seen(loaded):
    engine, assignment_id, user_id = loaded
    rng = random.Random(7)

    for _ in range(4):
        q = pick_next_question(engine, user_id, assignment_id, rng=rng)
        _record_history(engine, user_id, assignment_id, q)

    q = pick_next_question(engine, user_id, assignment_id, rng=rng)
    assert q is not None
    with engine.connect() as conn:
        all_qids = {
            r[0] for r in conn.execute(
                text("SELECT qid FROM questions WHERE assignment_id = :a"),
                {"a": assignment_id},
            ).fetchall()
        }
    assert q.qid in all_qids


def test_picker_is_deterministic_with_seeded_rng(loaded):
    engine, assignment_id, user_id = loaded

    def run(seed: int) -> list[str]:
        rng = random.Random(seed)
        out: list[str] = []
        # Use a fresh in-memory engine so history doesn't carry over between runs.
        for _ in range(4):
            q = pick_next_question(engine, user_id, assignment_id, rng=rng)
            out.append(q.qid)
            _record_history(engine, user_id, assignment_id, q)
        return out

    # Same seed against the same starting state should give the same first pick.
    rng_a = random.Random(123)
    rng_b = random.Random(123)
    qa = pick_next_question(engine, user_id, assignment_id, rng=rng_a)
    qb = pick_next_question(engine, user_id, assignment_id, rng=rng_b)
    assert qa.id == qb.id


def test_picker_separate_users_independent(tmp_path: Path):
    engine = _make_engine()
    path = tmp_path / "pick_demo.md"
    path.write_text(_FIXTURE, encoding="utf-8")
    load_assignment(engine, path)

    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        assignment_id = conn.execute(text("SELECT id FROM assignments")).scalar()
        u1 = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('u1','x','student',:c)"
            ),
            {"c": now},
        ).lastrowid
        u2 = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('u2','x','student',:c)"
            ),
            {"c": now},
        ).lastrowid

    rng = random.Random(0)
    q = pick_next_question(engine, u1, assignment_id, rng=rng)
    _record_history(engine, u1, assignment_id, q)

    # u2 still has all categories unseen → picker may pick from any of them.
    rng2 = random.Random(0)
    q2 = pick_next_question(engine, u2, assignment_id, rng=rng2)
    with engine.connect() as conn:
        seen_for_u2 = conn.execute(
            text("SELECT COUNT(*) FROM user_question_history WHERE user_id=:u"),
            {"u": u2},
        ).scalar()
    assert seen_for_u2 == 0
    assert q2 is not None
