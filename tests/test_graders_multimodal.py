"""Phase 5 — per-qtype grader tests against the LangGraph with a mocked router.

The router is a tiny scripted fake (one per qtype) that records the calls it
saw and returns a canned ``AIMessage`` so the graph compiles end-to-end and we
can assert on:

- which router roles were invoked, in order
- the messages the grader built (file path, source text, vision description)
- that the persisted submission has the right ``payload_kind`` and ``artifact_path``
- that the python grader never spawns an interpreter
"""

from __future__ import annotations

import base64
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.llm.grader import GraderState, build_grader_graph
from app.llm.verdicts import GradeVerdict


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    return e


def _seed(engine: Engine, qtype: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('alice', 'x', 'student', :c)"
            ),
            {"c": now},
        ).lastrowid
        a_id = conn.execute(
            text(
                "INSERT INTO assignments "
                "(slug, week, title, source_path, frontmatter_json, body_md, "
                " content_hash, max_credit_questions, loaded_at) "
                "VALUES ('a', 1, 'A', 'x', '{}', '', 'h', 1, :c)"
            ),
            {"c": now},
        ).lastrowid
        c_id = conn.execute(
            text(
                "INSERT INTO categories (assignment_id, name, ordering_index) "
                "VALUES (:a, 'cat', 0)"
            ),
            {"a": a_id},
        ).lastrowid
        q_id = conn.execute(
            text(
                "INSERT INTO questions "
                "(assignment_id, category_id, qid, qtype, prompt_md, rubric_md, max_attempts) "
                "VALUES (:a, :c, 'q', :qt, 'Build a histogram of net_income.', "
                " '- axis labels with units\n- bin count is reasonable', 6)"
            ),
            {"a": a_id, "c": c_id, "qt": qtype},
        ).lastrowid
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempts (user_id, question_id, started_at, status) "
                "VALUES (:u, :q, :s, 'in_progress')"
            ),
            {"u": user_id, "q": q_id, "s": now},
        ).lastrowid
    return {
        "user_id": user_id,
        "assignment_id": a_id,
        "category_id": c_id,
        "question_id": q_id,
        "attempt_id": attempt_id,
    }


def _load_attempt(engine, attempt_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, user_id, question_id, started_at, completed_at, "
                "       status, final_score, proof_token_id "
                "FROM attempts WHERE id = :id"
            ),
            {"id": attempt_id},
        ).fetchone()


def _load_question(engine, question_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id, assignment_id, category_id, qid, qtype, prompt_md, "
                "       rubric_md, max_attempts FROM questions WHERE id = :id"
            ),
            {"id": question_id},
        ).fetchone()


def _row_to_dict(row):
    return {k: getattr(row, k) for k in row._fields}


class ScriptedRouter:
    """Captures every ``invoke`` call and returns canned AIMessages."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.verdict_to_return: GradeVerdict | None = None
        self.vision_description: str = (
            "Description: histogram with x-axis 'Net Income ($M)', y-axis 'Count'.\n"
            "Rubric assessment: axis labels — yes; bin count — yes."
        )
        self.tutor_reply: str = "What is the bin width actually telling you?"

    def invoke(self, role, messages, *, response_schema=None, files=None,
               attempt_id=None, submission_id=None):
        self.calls.append({
            "role": role,
            "messages": messages,
            "response_schema": response_schema,
            "files": list(files) if files else None,
            "attempt_id": attempt_id,
        })
        if role == "vision":
            return AIMessage(content=self.vision_description)
        if role in {"grader", "code_judge", "excel_grader"}:
            return AIMessage(
                content="",
                additional_kwargs={"parsed": self.verdict_to_return},
            )
        if role == "tutor":
            return AIMessage(content=self.tutor_reply)
        raise KeyError(f"unexpected role: {role!r}")


# ---------------------------------------------------------------------------
# Image grader
# ---------------------------------------------------------------------------

# 1×1 PNG, base64-decoded — smallest valid PNG.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAA"
    "AAYAAjCB0C8AAAAASUVORK5CYII="
)


def test_image_grader_runs_vision_then_grader(engine, tmp_path):
    seed = _seed(engine, "image")
    img_path = tmp_path / "histogram.png"
    img_path.write_bytes(_TINY_PNG)

    fake = ScriptedRouter()
    fake.verdict_to_return = GradeVerdict(
        verdict="correct", score=1.0,
        rationale="Axes labeled; bins reasonable.",
        weakest_concept=None,
    )

    attempt = _row_to_dict(_load_attempt(engine, seed["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seed["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seed["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={
            "kind": "image",
            "artifact_path": str(img_path),
        },
        turn_index=1,
    )
    final = run(state)

    roles_called = [c["role"] for c in fake.calls]
    assert roles_called[0] == "vision"
    assert "grader" in roles_called
    assert final.vision_description.startswith("Description:")
    assert final.verdict.verdict == "correct"
    assert final.status_after == "passed"

    # Grader message should embed the vision description.
    grader_call = next(c for c in fake.calls if c["role"] == "grader")
    user_msg = next(m for m in grader_call["messages"] if m["role"] == "user")
    assert "Description: histogram" in user_msg["content"]

    # Submission persisted with payload_kind=image and the artifact path.
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT payload_kind, artifact_path FROM submissions "
                "WHERE attempt_id = :a"
            ),
            {"a": seed["attempt_id"]},
        ).fetchone()
    assert row.payload_kind == "image"
    assert row.artifact_path == str(img_path)


def test_image_grader_routes_through_tutor_on_partial(engine, tmp_path):
    seed = _seed(engine, "image")
    img_path = tmp_path / "histogram.png"
    img_path.write_bytes(_TINY_PNG)
    fake = ScriptedRouter()
    fake.verdict_to_return = GradeVerdict(
        verdict="partial", score=0.5,
        rationale="Axis labels present; bin count looks off.",
        weakest_concept="bin count",
    )

    attempt = _row_to_dict(_load_attempt(engine, seed["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seed["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seed["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={"kind": "image", "artifact_path": str(img_path)},
        turn_index=1,
    )
    final = run(state)
    assert final.tutor_reply, "tutor must reply on partial"
    roles = [c["role"] for c in fake.calls]
    assert "tutor" in roles


# ---------------------------------------------------------------------------
# Python code grader
# ---------------------------------------------------------------------------

def test_python_grader_never_executes_code(engine, tmp_path, monkeypatch):
    seed = _seed(engine, "python")
    source = (
        "import pandas as pd\n"
        "import matplotlib.pyplot as plt\n"
        "\n"
        "df = pd.read_csv('data/sp500_fundamentals.csv')\n"
        "latest = df[df['fiscal_year'] == df['fiscal_year'].max()]\n"
        "# bin choice: Freedman-Diaconis via numpy histogram_bin_edges\n"
        "plt.hist(latest['net_income'], bins='fd')\n"
        "plt.xlabel('Net Income ($M)')\n"
        "plt.ylabel('Count')\n"
        "plt.title('Net Income, latest fiscal year')\n"
        "plt.savefig('hist.png')\n"
    )
    py_path = tmp_path / "submission.py"
    py_path.write_text(source, encoding="utf-8")

    # Tripwires for any process spawn.
    import subprocess, os
    def _boom(*a, **k):
        raise AssertionError("python_code grader must NOT spawn a subprocess")
    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "call", _boom)
    monkeypatch.setattr(os, "system", _boom)
    if hasattr(os, "popen"):
        monkeypatch.setattr(os, "popen", _boom)

    fake = ScriptedRouter()
    fake.verdict_to_return = GradeVerdict(
        verdict="correct", score=1.0,
        rationale="Loads CSV, filters latest year, plots histogram with labels.",
        weakest_concept=None,
    )

    attempt = _row_to_dict(_load_attempt(engine, seed["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seed["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seed["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={
            "kind": "python",
            "artifact_path": str(py_path),
            "text": source,
        },
        turn_index=1,
    )
    final = run(state)
    assert final.verdict.verdict == "correct"

    # Only code_judge (and persist) were called — no "grader" role for python.
    judge_calls = [c for c in fake.calls if c["role"] == "code_judge"]
    assert len(judge_calls) == 1
    user_msg = next(m for m in judge_calls[0]["messages"] if m["role"] == "user")
    assert "plt.hist" in user_msg["content"]
    assert "NOT been executed" in user_msg["content"]


# ---------------------------------------------------------------------------
# Excel grader
# ---------------------------------------------------------------------------

def _make_fake_xlsx() -> bytes:
    """A valid-shape xlsx: a zip with the required OOXML top-level paths."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types/>',
        )
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/worksheets/sheet1.xml", "<sheet/>")
    return buf.getvalue()


def test_excel_grader_passes_path_via_files(engine, tmp_path):
    seed = _seed(engine, "excel")
    xlsx_path = tmp_path / "summary.xlsx"
    xlsx_path.write_bytes(_make_fake_xlsx())

    fake = ScriptedRouter()
    fake.verdict_to_return = GradeVerdict(
        verdict="correct", score=1.0,
        rationale="Summary sheet uses AVERAGEIF and has a labeled chart.",
        weakest_concept=None,
    )

    attempt = _row_to_dict(_load_attempt(engine, seed["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seed["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seed["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={
            "kind": "excel",
            "artifact_path": str(xlsx_path),
        },
        turn_index=1,
    )
    final = run(state)
    assert final.verdict.verdict == "correct"

    # The excel_grader role was invoked with files=[xlsx_path].
    excel_calls = [c for c in fake.calls if c["role"] == "excel_grader"]
    assert len(excel_calls) == 1
    assert excel_calls[0]["files"] == [str(xlsx_path)]


def test_excel_grader_raises_without_artifact_path(engine, tmp_path):
    seed = _seed(engine, "excel")
    fake = ScriptedRouter()
    fake.verdict_to_return = GradeVerdict(
        verdict="error", score=0.0, rationale="n/a",
    )

    attempt = _row_to_dict(_load_attempt(engine, seed["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seed["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seed["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={"kind": "excel"},  # missing artifact_path
        turn_index=1,
    )
    with pytest.raises(Exception):
        run(state)


# ---------------------------------------------------------------------------
# Anthropic provider files=
# ---------------------------------------------------------------------------

def test_anthropic_provider_uploads_files_when_present(monkeypatch, tmp_path):
    """The Anthropic provider should call client.beta.files.upload and
    attach container_upload blocks to the last user message."""
    from app.llm.providers.anthropic import AnthropicProvider

    xlsx_path = tmp_path / "wb.xlsx"
    xlsx_path.write_bytes(_make_fake_xlsx())

    captured: dict = {}

    class FakeUpload:
        id = "file_abc123"

    class FakeFiles:
        def upload(self, file, extra_headers=None):
            name, _fh, mime = file
            captured["upload_name"] = name
            captured["upload_mime"] = mime
            captured["upload_headers"] = extra_headers
            return FakeUpload()

    class FakeBeta:
        files = FakeFiles()

    class FakeContent:
        type = "tool_use"
        input = {"verdict": "correct", "score": 1.0, "rationale": "ok"}

    class FakeResponse:
        content = [FakeContent()]
        usage = None
        model = "claude-sonnet-4-6"
        stop_reason = "end_turn"

    class FakeMessages:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return FakeResponse()

    class FakeClient:
        beta = FakeBeta()
        messages = FakeMessages()

    p = AnthropicProvider(model="claude-sonnet-4-6", api_key="x")
    p._client = FakeClient()

    msg = p.invoke(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Grade my workbook."},
        ],
        response_schema=GradeVerdict,
        files=[str(xlsx_path)],
    )
    assert captured["upload_name"] == "wb.xlsx"
    assert "spreadsheetml" in captured["upload_mime"]
    # The xlsx-specific beta header must be present on the upload call.
    assert "files-api" in captured["upload_headers"]["anthropic-beta"]

    user_blocks = captured["kwargs"]["messages"][-1]["content"]
    assert isinstance(user_blocks, list)
    assert any(b.get("type") == "container_upload" and b.get("file_id") == "file_abc123"
               for b in user_blocks)
    # extra_headers carries both the files and skills beta flags.
    hdr = captured["kwargs"]["extra_headers"]["anthropic-beta"]
    assert "files-api" in hdr and "skills" in hdr

    assert isinstance(msg.additional_kwargs.get("parsed"), GradeVerdict)
    assert msg.additional_kwargs["parsed"].verdict == "correct"
