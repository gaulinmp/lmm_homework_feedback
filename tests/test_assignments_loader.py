from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.assignments_loader import load_assignment


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


@pytest.fixture
def engine() -> Engine:
    return _make_engine()


_FIXTURE = """\
---
slug: demo
week: 1
title: "Demo Assignment"
max_credit_questions: 3
categories:
  - name: alpha
  - name: beta
---

## Question q1

```yaml
qid: q1
category: alpha
type: text
max_attempts: 4
rubric: |
  - Be correct
```

Write a paragraph about alpha.

## Question q2

```yaml
qid: q2
category: beta
type: image
max_attempts: 5
rubric: |
  - Show a chart
```

Upload an image showing beta.
"""


def _write(tmp_path: Path, body: str, name: str = "demo.md") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_load_inserts_expected_rows(tmp_path: Path, engine: Engine):
    path = _write(tmp_path, _FIXTURE)
    assert load_assignment(engine, path) == "added"

    with engine.connect() as conn:
        a = conn.execute(text("SELECT * FROM assignments")).mappings().all()
        cats = conn.execute(
            text("SELECT name, ordering_index FROM categories ORDER BY ordering_index")
        ).all()
        qs = conn.execute(
            text(
                "SELECT qid, qtype, prompt_md, rubric_md, max_attempts, category_id "
                "FROM questions ORDER BY qid"
            )
        ).mappings().all()

    assert len(a) == 1
    row = a[0]
    assert row["slug"] == "demo"
    assert row["week"] == 1
    assert row["title"] == "Demo Assignment"
    assert row["max_credit_questions"] == 3
    assert len(row["content_hash"]) == 64

    assert [c.name for c in cats] == ["alpha", "beta"]
    assert [c.ordering_index for c in cats] == [0, 1]

    assert len(qs) == 2
    q1 = next(q for q in qs if q["qid"] == "q1")
    q2 = next(q for q in qs if q["qid"] == "q2")
    assert q1["qtype"] == "text"
    assert q1["max_attempts"] == 4
    assert "alpha" in q1["prompt_md"].lower()
    assert "Be correct" in q1["rubric_md"]
    assert q2["qtype"] == "image"
    assert q2["max_attempts"] == 5
    assert "beta" in q2["prompt_md"].lower()


def test_load_is_idempotent(tmp_path: Path, engine: Engine):
    path = _write(tmp_path, _FIXTURE)
    assert load_assignment(engine, path) == "added"
    assert load_assignment(engine, path) == "skipped"
    assert load_assignment(engine, path) == "skipped"

    with engine.connect() as conn:
        n_q = conn.execute(text("SELECT COUNT(*) FROM questions")).scalar()
        n_c = conn.execute(text("SELECT COUNT(*) FROM categories")).scalar()
        n_a = conn.execute(text("SELECT COUNT(*) FROM assignments")).scalar()
    assert (n_a, n_c, n_q) == (1, 2, 2)


def test_load_updates_when_content_changes(tmp_path: Path, engine: Engine):
    path = _write(tmp_path, _FIXTURE)
    assert load_assignment(engine, path) == "added"
    new_body = _FIXTURE.replace("Write a paragraph about alpha.", "Write TWO paragraphs about alpha.")
    path.write_text(new_body, encoding="utf-8")
    assert load_assignment(engine, path) == "updated"

    with engine.connect() as conn:
        prompt = conn.execute(
            text("SELECT prompt_md FROM questions WHERE qid='q1'")
        ).scalar()
    assert "TWO paragraphs" in prompt


def test_invalid_qtype_raises(tmp_path: Path, engine: Engine):
    bad = _FIXTURE.replace("type: text", "type: video")
    path = _write(tmp_path, bad, name="bad_qtype.md")
    with pytest.raises(ValueError, match="invalid qtype"):
        load_assignment(engine, path)
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM assignments")).scalar()
    assert n == 0


def test_unknown_category_raises(tmp_path: Path, engine: Engine):
    bad = _FIXTURE.replace("category: beta", "category: gamma")
    path = _write(tmp_path, bad, name="bad_cat.md")
    with pytest.raises(ValueError, match="undeclared category"):
        load_assignment(engine, path)


def test_repo_assignment_loads(engine: Engine):
    repo_file = Path(__file__).resolve().parent.parent / "assignments" / "week3_visualization.md"
    if not repo_file.exists():
        pytest.skip("week3_visualization.md not present")
    assert load_assignment(engine, repo_file) == "added"
    with engine.connect() as conn:
        n_cats = conn.execute(text("SELECT COUNT(*) FROM categories")).scalar()
        n_qs = conn.execute(text("SELECT COUNT(*) FROM questions")).scalar()
    assert n_cats == 5
    assert n_qs == 9
