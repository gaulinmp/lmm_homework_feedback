from sqlalchemy import create_engine, event, text


EXPECTED_TABLES = {
    "users",
    "assignments",
    "categories",
    "questions",
    "attempts",
    "user_question_history",
    "submissions",
    "llm_messages",
    "proof_tokens",
    "sessions",
}


def test_init_db_creates_all_tables(tmp_path, monkeypatch):
    db_file = tmp_path / "tutor.db"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(db_file))

    # Re-import config + db with patched env so settings pick up tmp_path.
    import importlib
    from app import config as config_module
    importlib.reload(config_module)
    from app import db as db_module
    importlib.reload(db_module)

    db_module.init_db()

    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    names = {r[0] for r in rows}
    assert EXPECTED_TABLES.issubset(names), names
