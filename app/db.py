from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy import create_engine

from app.config import settings


SCHEMA_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS users (
      id              INTEGER PRIMARY KEY,
      username        TEXT UNIQUE NOT NULL,
      password_hash   TEXT NOT NULL,
      role            TEXT NOT NULL CHECK (role IN ('student','admin')),
      created_at      TEXT NOT NULL,
      canvas_user_id  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assignments (
      id                     INTEGER PRIMARY KEY,
      slug                   TEXT UNIQUE NOT NULL,
      week                   INTEGER,
      title                  TEXT NOT NULL,
      source_path            TEXT NOT NULL,
      frontmatter_json       TEXT NOT NULL,
      body_md                TEXT NOT NULL,
      content_hash           TEXT NOT NULL,
      max_credit_questions   INTEGER NOT NULL DEFAULT 1,
      loaded_at              TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS categories (
      id              INTEGER PRIMARY KEY,
      assignment_id   INTEGER NOT NULL REFERENCES assignments(id),
      name            TEXT NOT NULL,
      ordering_index  INTEGER NOT NULL,
      UNIQUE (assignment_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS questions (
      id                       INTEGER PRIMARY KEY,
      assignment_id            INTEGER NOT NULL REFERENCES assignments(id),
      category_id              INTEGER NOT NULL REFERENCES categories(id),
      qid                      TEXT NOT NULL,
      qtype                    TEXT NOT NULL CHECK (qtype IN ('text','image','python','excel')),
      prompt_md                TEXT NOT NULL,
      rubric_md                TEXT NOT NULL,
      reference_solution_md    TEXT,
      data_files_json          TEXT NOT NULL DEFAULT '[]',
      max_attempts             INTEGER NOT NULL DEFAULT 6,
      metadata_json            TEXT NOT NULL DEFAULT '{}',
      UNIQUE (assignment_id, qid)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attempts (
      id                INTEGER PRIMARY KEY,
      user_id           INTEGER NOT NULL REFERENCES users(id),
      question_id       INTEGER NOT NULL REFERENCES questions(id),
      started_at        TEXT NOT NULL,
      completed_at      TEXT,
      status            TEXT NOT NULL CHECK (status IN ('in_progress','passed','abandoned','exhausted')),
      final_score       REAL,
      proof_token_id    INTEGER REFERENCES proof_tokens(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_question_history (
      id              INTEGER PRIMARY KEY,
      user_id         INTEGER NOT NULL REFERENCES users(id),
      assignment_id   INTEGER NOT NULL REFERENCES assignments(id),
      category_id     INTEGER NOT NULL REFERENCES categories(id),
      question_id     INTEGER NOT NULL REFERENCES questions(id),
      attempt_id      INTEGER NOT NULL REFERENCES attempts(id),
      completed_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS submissions (
      id                INTEGER PRIMARY KEY,
      attempt_id        INTEGER NOT NULL REFERENCES attempts(id),
      turn_index        INTEGER NOT NULL,
      submitted_at      TEXT NOT NULL,
      payload_kind      TEXT NOT NULL CHECK (payload_kind IN ('text','image','python','excel')),
      payload_text      TEXT,
      artifact_path     TEXT,
      grader_verdict    TEXT NOT NULL CHECK (grader_verdict IN ('correct','partial','incorrect','error')),
      grader_score      REAL,
      grader_rationale  TEXT NOT NULL,
      tutor_reply_md    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_messages (
      id              INTEGER PRIMARY KEY,
      attempt_id      INTEGER REFERENCES attempts(id),
      submission_id   INTEGER REFERENCES submissions(id),
      role_bucket     TEXT NOT NULL,
      provider        TEXT NOT NULL,
      model           TEXT NOT NULL,
      role            TEXT NOT NULL,
      content         TEXT NOT NULL,
      tool_name       TEXT,
      tool_args_json  TEXT,
      tokens_in       INTEGER,
      tokens_out      INTEGER,
      latency_ms      INTEGER,
      created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS proof_tokens (
      id                INTEGER PRIMARY KEY,
      attempt_id        INTEGER UNIQUE NOT NULL REFERENCES attempts(id),
      payload_json      TEXT NOT NULL,
      hmac_sig          TEXT NOT NULL,
      issued_at         TEXT NOT NULL,
      printed_at        TEXT,
      canvas_posted_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
      id          TEXT PRIMARY KEY,
      user_id     INTEGER NOT NULL REFERENCES users(id),
      created_at  TEXT NOT NULL,
      expires_at  TEXT NOT NULL,
      revoked_at  TEXT
    )
    """,
]


def _engine_url() -> str:
    return f"sqlite:///{settings.DB_PATH}"


def make_engine() -> Engine:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(_engine_url(), future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def init_db() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.exec_driver_sql(stmt)
