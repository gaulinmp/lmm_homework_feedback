"""Phase 7 — tutor leakage guardrail.

The tutor node must never emit reference-solution tokens verbatim. We
synthesize a leak by handing the fake router a tutor response that contains
a number lifted straight out of ``reference_solution_md`` and assert that:

- the leakage regex catches it
- the tutor node regenerates exactly once with a stricter prompt
- if the regeneration also leaks, the canned fallback reply is used
- a clean reply passes through unchanged
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from app import db as db_module
from app.llm.grader import (
    GraderState,
    build_grader_graph,
    extract_reference_tokens,
    find_leaked_tokens,
)
from app.llm.verdicts import GradeVerdict


# ---------------------------------------------------------------------------
# Direct unit tests on the leak-detection helpers
# ---------------------------------------------------------------------------

def test_extract_reference_tokens_pulls_numbers_and_identifiers():
    ref = "Use `pd.read_csv('orders.csv')` then `groupby('region')`. Answer: 42.7"
    tokens = extract_reference_tokens(ref)
    # numeric leak signal — should be present
    assert "42.7" in tokens
    # identifier-shaped tokens worth flagging
    assert "groupby" in tokens
    assert "read_csv" in tokens
    assert "region" in tokens
    # filtered english stopwords should be dropped
    assert "answer" not in tokens
    assert "use" not in tokens


def test_extract_reference_tokens_handles_empty():
    assert extract_reference_tokens(None) == set()
    assert extract_reference_tokens("") == set()


def test_find_leaked_tokens_whole_word():
    ref_tokens = {"42.7", "groupby", "region"}
    # whole-word leak
    assert find_leaked_tokens("the answer is 42.7", ref_tokens) == {"42.7"}
    # substring should NOT count as a leak (grouping vs groupby)
    assert find_leaked_tokens("try grouping the data", ref_tokens) == set()
    # case-insensitive match
    assert find_leaked_tokens("Use GROUPBY here.", ref_tokens) == {"groupby"}


def test_find_leaked_tokens_empty_inputs():
    assert find_leaked_tokens("anything", set()) == set()
    assert find_leaked_tokens("", {"x"}) == set()


# ---------------------------------------------------------------------------
# End-to-end guardrail behaviour through the grader graph
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


@pytest.fixture
def seeded(engine):
    """Seed a user + assignment + question with a known reference solution."""
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        user_id = conn.execute(
            text(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES ('alice', 'x', 'student', :c)"
            ),
            {"c": now},
        ).lastrowid
        assignment_id = conn.execute(
            text(
                "INSERT INTO assignments "
                "(slug, week, title, source_path, frontmatter_json, body_md, "
                " content_hash, max_credit_questions, loaded_at) "
                "VALUES ('demo', 1, 'Demo', 'x', '{}', '', 'h', 1, :c)"
            ),
            {"c": now},
        ).lastrowid
        cat_id = conn.execute(
            text(
                "INSERT INTO categories (assignment_id, name, ordering_index) "
                "VALUES (:a, 'cat', 0)"
            ),
            {"a": assignment_id},
        ).lastrowid
        question_id = conn.execute(
            text(
                "INSERT INTO questions "
                "(assignment_id, category_id, qid, qtype, prompt_md, rubric_md, "
                " reference_solution_md, max_attempts) "
                "VALUES (:a, :c, 'q1', 'text', 'Compute the answer.', "
                " '- shows the right number', "
                " 'The reference answer is 42.7 using groupby on region.', "
                " 3)"
            ),
            {"a": assignment_id, "c": cat_id},
        ).lastrowid
        attempt_id = conn.execute(
            text(
                "INSERT INTO attempts "
                "(user_id, question_id, started_at, status) "
                "VALUES (:u, :q, :s, 'in_progress')"
            ),
            {"u": user_id, "q": question_id, "s": now},
        ).lastrowid
    return {
        "user_id": user_id,
        "question_id": question_id,
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
                "SELECT id, assignment_id, category_id, qid, qtype, "
                "       prompt_md, rubric_md, reference_solution_md, "
                "       max_attempts "
                "FROM questions WHERE id = :id"
            ),
            {"id": question_id},
        ).fetchone()


def _row_to_dict(row):
    return {k: getattr(row, k) for k in row._fields}


class GuardrailRouter:
    """Fake router that scripts grader + tutor responses in order.

    ``tutor_replies`` is a list of strings; the tutor role pops them in order
    so the test can simulate "first reply leaks, regen succeeds" or "both leak".
    """

    def __init__(self, *, verdict: GradeVerdict, tutor_replies: list[str]):
        self.verdict = verdict
        self.tutor_replies = list(tutor_replies)
        self.tutor_calls: list[list] = []

    def invoke(self, role, messages, *, response_schema=None, files=None,
               attempt_id=None, submission_id=None):
        if role == "grader":
            return AIMessage(content="", additional_kwargs={"parsed": self.verdict})
        if role == "tutor":
            self.tutor_calls.append(messages)
            if not self.tutor_replies:
                return AIMessage(content="(no more scripted tutor replies)")
            return AIMessage(content=self.tutor_replies.pop(0))
        raise KeyError(f"unexpected role: {role!r}")


def test_tutor_leak_caught_and_regenerated(engine, seeded):
    """First tutor reply leaks '42.7' verbatim → regen produces a clean reply."""
    fake = GuardrailRouter(
        verdict=GradeVerdict(
            verdict="partial",
            score=0.4,
            rationale="Missing the headline number.",
            weakest_concept="aggregation",
        ),
        tutor_replies=[
            # leak: the literal answer number from reference_solution_md
            "Close — but consider what you get when you compute 42.7 here.",
            # clean retry that avoids the leaked tokens
            "Close — but think about what kind of aggregation matches the "
            "rubric here.",
        ],
    )

    attempt = _row_to_dict(_load_attempt(engine, seeded["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seeded["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seeded["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={"kind": "text", "text": "wrong answer"},
        turn_index=1,
    )
    final = run(state)

    # Two tutor invocations: original + regen with stricter prompt
    assert len(fake.tutor_calls) == 2
    stricter_msg = fake.tutor_calls[1][-1]
    assert "42.7" in stricter_msg["content"]
    assert "Regenerate" in stricter_msg["content"]

    # Stored tutor reply is the clean second response, NOT the leaked first one
    assert final.tutor_reply is not None
    assert "42.7" not in final.tutor_reply
    assert "aggregation" in final.tutor_reply


def test_tutor_persistent_leak_falls_back_to_canned(engine, seeded):
    """If the regeneration ALSO leaks, the canned fallback reply is used."""
    fake = GuardrailRouter(
        verdict=GradeVerdict(
            verdict="incorrect",
            score=0.0,
            rationale="Wrong technique.",
            weakest_concept="aggregation",
        ),
        tutor_replies=[
            "Try computing 42.7 with groupby.",  # leaks both
            "Still: just use 42.7 directly.",     # still leaks
        ],
    )

    attempt = _row_to_dict(_load_attempt(engine, seeded["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seeded["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seeded["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={"kind": "text", "text": "wrong"},
        turn_index=1,
    )
    final = run(state)

    assert len(fake.tutor_calls) == 2  # one regen, then fallback (no third call)
    assert final.tutor_reply is not None
    assert "42.7" not in final.tutor_reply
    assert "groupby" not in final.tutor_reply
    # Canned message hint
    assert "different approach" in final.tutor_reply.lower()


def test_clean_tutor_reply_passes_through_unchanged(engine, seeded):
    fake = GuardrailRouter(
        verdict=GradeVerdict(
            verdict="partial",
            score=0.5,
            rationale="Almost there.",
            weakest_concept="aggregation",
        ),
        tutor_replies=[
            "What kind of summary would a rubric-graded answer require here?",
        ],
    )

    attempt = _row_to_dict(_load_attempt(engine, seeded["attempt_id"]))
    question = _row_to_dict(_load_question(engine, seeded["question_id"]))
    run = build_grader_graph(fake, engine, user_id=seeded["user_id"])
    state = GraderState(
        attempt=attempt,
        question=question,
        submission_payload={"kind": "text", "text": "wrong"},
        turn_index=1,
    )
    final = run(state)

    # Only one tutor call — no regen needed
    assert len(fake.tutor_calls) == 1
    assert final.tutor_reply == (
        "What kind of summary would a rubric-graded answer require here?"
    )
