"""Phase 5 — upload validation tests.

Covers ``app.uploads`` (pure validator + content-addressed storage) and the
``POST /attempts/{id}/submit`` route's behavior when fed an upload that fails
the magic-byte check.
"""

from __future__ import annotations

import base64
import zipfile
from datetime import datetime, timezone
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.auth import LocalAuthBackend, auth_rate_limit, csrf_token_for
from app.llm.verdicts import GradeVerdict
from app.main import app
from app.routes import student as student_routes
from app.uploads import (
    UploadError,
    store_upload,
    validate_and_store,
    validate_payload,
)


# ---------------------------------------------------------------------------
# Sample byte strings
# ---------------------------------------------------------------------------

_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAA"
    "AAYAAjCB0C8AAAAASUVORK5CYII="
)
_TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605"
    "08070707090908"
) + b"\x00" * 64 + b"\xff\xd9"
_EXE_MZ = b"MZ\x90\x00" + b"\x00" * 100


def _make_fake_xlsx() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
        z.writestr("xl/worksheets/sheet1.xml", "<sheet/>")
    return buf.getvalue()


def _make_plain_zip() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("hello.txt", "not an xlsx")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Pure validator
# ---------------------------------------------------------------------------

class TestValidatePayload:
    def test_png_accepted(self):
        ext, _ = validate_payload("image", "x.png", _TINY_PNG)
        assert ext == ".png"

    def test_jpeg_accepted(self):
        ext, _ = validate_payload("image", "x.jpg", _TINY_JPEG)
        assert ext == ".jpg"

    def test_image_rejects_zip_renamed_to_png(self):
        with pytest.raises(UploadError, match="PNG or JPEG signature"):
            validate_payload("image", "evil.png", _make_plain_zip())

    def test_image_rejects_exe_renamed_to_png(self):
        with pytest.raises(UploadError, match="PNG or JPEG signature"):
            validate_payload("image", "evil.png", _EXE_MZ)

    def test_xlsx_accepted(self):
        ext, _ = validate_payload("excel", "wb.xlsx", _make_fake_xlsx())
        assert ext == ".xlsx"

    def test_xlsx_rejects_zip_without_xl_dir(self):
        with pytest.raises(UploadError, match="does not look like an .xlsx"):
            validate_payload("excel", "fake.xlsx", _make_plain_zip())

    def test_xlsx_rejects_exe_renamed(self):
        with pytest.raises(UploadError, match="ZIP signature"):
            validate_payload("excel", "evil.xlsx", _EXE_MZ)

    def test_python_accepted(self):
        ext, decoded = validate_payload(
            "python", "s.py", b"import pandas as pd\nprint('ok')\n"
        )
        assert ext == ".py"
        assert "pandas" in decoded

    def test_python_rejects_zip(self):
        with pytest.raises(UploadError, match="binary signature|NUL|UTF-8"):
            validate_payload("python", "s.py", _make_plain_zip())

    def test_python_rejects_exe(self):
        with pytest.raises(UploadError, match="binary signature|NUL|UTF-8"):
            validate_payload("python", "s.py", _EXE_MZ)

    def test_python_rejects_non_utf8(self):
        with pytest.raises(UploadError, match="UTF-8"):
            validate_payload("python", "s.py", b"\xff\xfe\xfd")

    def test_size_cap_image(self, monkeypatch):
        # Cap to 64 bytes to avoid building a 2MB blob in test.
        from app import uploads as up_mod
        monkeypatch.setitem(
            up_mod._QTYPE_LIMITS, "image", (64, ".png", "image")
        )
        with pytest.raises(UploadError, match="too large"):
            validate_payload("image", "x.png", _TINY_PNG * 10)

    def test_empty_rejected(self):
        with pytest.raises(UploadError, match="empty"):
            validate_payload("image", "x.png", b"")

    def test_unknown_qtype_rejected(self):
        with pytest.raises(UploadError, match="not an upload type"):
            validate_payload("text", "x.txt", b"hello")


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------

def test_store_upload_uses_hashed_path(tmp_path):
    data = _TINY_PNG
    stored = store_upload("image", data, ".png", uploads_dir=tmp_path)
    assert stored.path.parent.parent == tmp_path
    assert stored.path.parent.name == stored.sha256[:2]
    assert stored.path.name == f"{stored.sha256}.png"
    assert stored.path.read_bytes() == data


def test_store_upload_dedupes_identical_bytes(tmp_path):
    data = _TINY_PNG
    a = store_upload("image", data, ".png", uploads_dir=tmp_path)
    b = store_upload("image", data, ".png", uploads_dir=tmp_path)
    assert a.path == b.path
    # Only one file under the hashed directory.
    assert list(a.path.parent.iterdir()) == [a.path]


def test_validate_and_store_combined(tmp_path):
    stored = validate_and_store(
        "python",
        "s.py",
        b"import pandas as pd\n",
        uploads_dir=tmp_path,
    )
    assert stored.ext == ".py"
    assert stored.text == "import pandas as pd\n"
    assert stored.path.exists()


# ---------------------------------------------------------------------------
# HTTP route — magic-byte rejection at /attempts/{id}/submit
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


class _SilentRouter:
    """Tests of *rejection* paths should never reach an LLM call."""

    def invoke(self, role, messages, *, response_schema=None, files=None,
               attempt_id=None, submission_id=None):
        raise AssertionError(
            f"router.invoke must not be called for rejected upload "
            f"(role={role!r})"
        )


@pytest.fixture
def http(monkeypatch, tmp_path):
    engine = _make_engine()
    monkeypatch.setattr(db_module, "_engine", engine)
    auth_rate_limit.reset()

    # Send uploads to a tmp dir, not the real data/uploads/.
    from app import uploads as up_mod
    monkeypatch.setattr(up_mod, "UPLOADS_DIR", tmp_path / "uploads")

    # Replace the router so any accidental LLM call would explode.
    monkeypatch.setattr(student_routes, "_router_singleton", _SilentRouter())
    monkeypatch.setattr(student_routes, "get_router", lambda: _SilentRouter())

    LocalAuthBackend(engine).create_user("alice", "pw-12345", role="student")

    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
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
        ids = {}
        for qtype in ("image", "python", "excel"):
            q = conn.execute(
                text(
                    "INSERT INTO questions "
                    "(assignment_id, category_id, qid, qtype, prompt_md, rubric_md, max_attempts) "
                    "VALUES (:a, :c, :qid, :qt, 'p', 'r', 5)"
                ),
                {"a": a_id, "c": c_id, "qid": f"q_{qtype}", "qt": qtype},
            ).lastrowid
            ids[qtype] = q

        user_row = conn.execute(
            text("SELECT id FROM users WHERE username='alice'")
        ).fetchone()
        attempts = {}
        for qtype, q_id in ids.items():
            att = conn.execute(
                text(
                    "INSERT INTO attempts (user_id, question_id, started_at, status) "
                    "VALUES (:u, :q, :s, 'in_progress')"
                ),
                {"u": user_row.id, "q": q_id, "s": now},
            ).lastrowid
            attempts[qtype] = att

    client = TestClient(app)
    r = client.post(
        "/login",
        data={"username": "alice", "password": "pw-12345"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    sid = client.cookies.get("tutor_session")
    csrf = csrf_token_for(sid)
    return client, csrf, attempts, engine


def _post_upload(client, csrf, attempt_id, filename, content_type, data):
    return client.post(
        f"/attempts/{attempt_id}/submit",
        files={"submission_file": (filename, data, content_type)},
        headers={"X-CSRF-Token": csrf},
    )


def test_route_rejects_exe_renamed_xlsx(http):
    client, csrf, attempts, _ = http
    r = _post_upload(
        client, csrf, attempts["excel"],
        "submission.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _EXE_MZ,
    )
    assert r.status_code == 400
    assert "ZIP signature" in r.text


def test_route_rejects_zip_renamed_png(http):
    client, csrf, attempts, _ = http
    r = _post_upload(
        client, csrf, attempts["image"],
        "evil.png", "image/png", _make_plain_zip(),
    )
    assert r.status_code == 400
    assert "PNG or JPEG signature" in r.text


def test_route_rejects_binary_python(http):
    client, csrf, attempts, _ = http
    r = _post_upload(
        client, csrf, attempts["python"],
        "evil.py", "text/x-python", _EXE_MZ,
    )
    assert r.status_code == 400


def test_route_accepts_valid_png_and_stores_under_hashed_path(http, monkeypatch, tmp_path):
    """Happy-path upload of a PNG should call the router and persist the
    submission with payload_kind=image and an artifact path under uploads/."""
    client, csrf, attempts, engine = http

    # Swap the silent router for a scripted one that returns a correct verdict.
    class ScriptedRouter:
        def __init__(self):
            self.calls = []
            self.verdict = GradeVerdict(
                verdict="correct", score=1.0,
                rationale="Axis labels and bin count look reasonable.",
                weakest_concept=None,
            )

        def invoke(self, role, messages, *, response_schema=None, files=None,
                   attempt_id=None, submission_id=None):
            self.calls.append(role)
            if role == "vision":
                return AIMessage(content="Description: histogram. Rubric: yes/yes")
            if role == "grader":
                return AIMessage(
                    content="",
                    additional_kwargs={"parsed": self.verdict},
                )
            if role == "tutor":
                return AIMessage(content="...")
            raise KeyError(role)

    fake = ScriptedRouter()
    monkeypatch.setattr(student_routes, "_router_singleton", fake)
    monkeypatch.setattr(student_routes, "get_router", lambda: fake)

    r = _post_upload(
        client, csrf, attempts["image"],
        "histogram.png", "image/png", _TINY_PNG,
    )
    assert r.status_code == 200, r.text
    assert "vision" in fake.calls
    assert "grader" in fake.calls

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT payload_kind, artifact_path, grader_verdict "
                "FROM submissions WHERE attempt_id=:a"
            ),
            {"a": attempts["image"]},
        ).fetchone()
    assert row.payload_kind == "image"
    assert row.grader_verdict == "correct"
    # Path uses the sha256/<sha> hashed layout under our tmp uploads dir.
    assert "uploads" in row.artifact_path
    import hashlib
    digest = hashlib.sha256(_TINY_PNG).hexdigest()
    assert digest in row.artifact_path


def test_route_missing_file_returns_400(http):
    client, csrf, attempts, _ = http
    r = client.post(
        f"/attempts/{attempts['image']}/submit",
        data={},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 400


def test_route_text_qtype_still_works(http, monkeypatch):
    """Phase-4 text submission must keep working alongside the new upload code."""
    client, csrf, _attempts, engine = http

    # Add a text-type question + attempt for alice.
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        a_id = conn.execute(
            text("SELECT id FROM assignments LIMIT 1")
        ).fetchone().id
        cat_id = conn.execute(
            text("SELECT id FROM categories WHERE assignment_id=:a"),
            {"a": a_id},
        ).fetchone().id
        q_id = conn.execute(
            text(
                "INSERT INTO questions "
                "(assignment_id, category_id, qid, qtype, prompt_md, rubric_md, max_attempts) "
                "VALUES (:a, :c, 'tq', 'text', 'p', 'r', 5)"
            ),
            {"a": a_id, "c": cat_id},
        ).lastrowid
        user_id = conn.execute(
            text("SELECT id FROM users WHERE username='alice'")
        ).fetchone().id
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempts (user_id, question_id, started_at, status) "
                "VALUES (:u, :q, :s, 'in_progress')"
            ),
            {"u": user_id, "q": q_id, "s": now},
        ).lastrowid

    class ScriptedRouter:
        def __init__(self):
            self.calls = []
        def invoke(self, role, messages, *, response_schema=None, files=None,
                   attempt_id=None, submission_id=None):
            self.calls.append(role)
            if role == "grader":
                return AIMessage(content="", additional_kwargs={"parsed": GradeVerdict(
                    verdict="correct", score=1.0, rationale="ok"
                )})
            return AIMessage(content="...")

    fake = ScriptedRouter()
    monkeypatch.setattr(student_routes, "_router_singleton", fake)
    monkeypatch.setattr(student_routes, "get_router", lambda: fake)

    r = client.post(
        f"/attempts/{attempt_id}/submit",
        data={"student_text": "I would mention shape and outliers."},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.text
    assert "grader" in fake.calls
    assert "vision" not in fake.calls
