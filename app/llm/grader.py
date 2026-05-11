"""LangGraph state machine for the Socratic grading loop.

Five deterministic nodes per submission, per §7 of the design doc::

    prepare → preprocess → grade → tutor → persist
                                    ↑ (skipped on verdict == correct)

This phase wires up the *text* qtype only. ``preprocess`` is a no-op for text;
phase 5 will fill it in for image/excel. The graph shape stays constant across
qtypes so phase 5 only adds work to that one node.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.graders.excel import build_excel_grader_messages
from app.graders.image import (
    build_grader_messages_from_vision,
    build_vision_messages,
)
from app.graders.python_code import build_code_judge_messages
from app.graders.text import PriorTurn, build_grader_messages
from app.llm.prompts import TUTOR_SYSTEM_PROMPT
from app.llm.router import LLMRouter
from app.llm.verdicts import GradeVerdict
from app.proof import mint as mint_proof_token


logger = logging.getLogger(__name__)


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]{2,}|\d+(?:\.\d+)?")

# Common English / domain words that aren't meaningful leaks even when they
# appear in the reference solution. Tutor replies routinely use these.
_LEAK_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "from", "this", "that", "have", "has",
    "are", "you", "your", "they", "their", "them", "but", "not", "any",
    "can", "will", "would", "should", "could", "may", "might", "must",
    "use", "uses", "used", "using", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine", "ten", "zero",
    "data", "value", "values", "column", "row", "rows", "table",
    "chart", "plot", "graph", "answer", "question", "rubric",
    "student", "tutor", "grader", "reference", "solution",
    "true", "false", "none", "null", "first", "second", "third",
    "into", "than", "then", "when", "what", "which", "where", "why",
    "how", "all", "some", "more", "most", "less", "very", "just",
    "also", "only", "even", "such", "each", "both", "many", "much",
    "every", "while", "because", "since", "though", "although",
    "between", "across", "above", "below", "after", "before",
    "consider", "approach", "think", "make", "see", "show", "tell",
})


_CANNED_TUTOR_REPLY = (
    "Let me try a different approach — what part of the rubric do you find "
    "most confusing?"
)


def extract_reference_tokens(reference_md: str | None) -> set[str]:
    """Pull out numeric and identifier-shaped tokens from the reference.

    Tokens are normalised to lowercase. Short tokens, plain English
    function words, and tokens with < 3 chars are dropped because they
    generate too much noise. Numbers are always kept (they're the highest
    signal indicator of a leaked reference value).
    """
    if not reference_md:
        return set()
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(reference_md):
        tok = raw.lower()
        if tok[0].isdigit():
            # numeric — keep even short ones, they're high-signal
            out.add(tok)
            continue
        if len(tok) < 3:
            continue
        if tok in _LEAK_STOPWORDS:
            continue
        out.add(tok)
    return out


def find_leaked_tokens(reply: str | None, reference_tokens: set[str]) -> set[str]:
    """Return reference tokens that appear verbatim (whole-word) in the reply."""
    if not reply or not reference_tokens:
        return set()
    reply_lower = reply.lower()
    leaks: set[str] = set()
    for tok in reference_tokens:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(tok)}(?![A-Za-z0-9_])", reply_lower):
            leaks.add(tok)
    return leaks


class GraderState(BaseModel):
    """State carried through the LangGraph nodes for one submission."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    attempt: dict[str, Any]
    question: dict[str, Any]
    submission_payload: dict[str, Any]
    turn_index: int

    verdict: Optional[GradeVerdict] = None
    tutor_reply: Optional[str] = None
    submission_id: Optional[int] = None
    status_after: Optional[str] = None
    prior_turns: list[PriorTurn] = Field(default_factory=list)
    vision_description: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_prior_turns(engine: Engine, attempt_id: int) -> list[PriorTurn]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT turn_index, payload_text, grader_verdict, grader_rationale "
                "FROM submissions "
                "WHERE attempt_id = :a "
                "ORDER BY turn_index ASC"
            ),
            {"a": attempt_id},
        ).fetchall()
    return [
        PriorTurn(
            turn_index=r.turn_index,
            student_text=r.payload_text or "",
            grader_verdict=r.grader_verdict,
            grader_rationale=r.grader_rationale or "",
        )
        for r in rows
    ]


def _build_tutor_messages(
    *,
    question_prompt: str,
    rubric: str,
    student_text: str,
    verdict: GradeVerdict,
    prior_turns: list[PriorTurn],
    attempt_index: int,
) -> list[dict[str, str]]:
    if prior_turns:
        history_lines = []
        for t in prior_turns:
            history_lines.append(
                f"Attempt {t.turn_index} — verdict: {t.grader_verdict}\n"
                f"Student wrote: {t.student_text}\n"
                f"Grader said: {t.grader_rationale}"
            )
        history = "\n\n".join(history_lines)
    else:
        history = "(no prior turns — this is the first submission)"

    user_content = (
        f"Attempt index: {attempt_index} (1 = first try)\n\n"
        "Question prompt:\n"
        f"{question_prompt.strip()}\n\n"
        "Rubric (do not reveal verbatim):\n"
        f"{rubric.strip()}\n\n"
        "Prior attempts on this question:\n"
        f"{history}\n\n"
        "Student's latest submission:\n"
        f"{student_text.strip()}\n\n"
        "Grader verdict: "
        f"{verdict.verdict} (score={verdict.score})\n"
        f"Grader rationale: {verdict.rationale}\n"
        f"Weakest concept: {verdict.weakest_concept or '(none specified)'}\n\n"
        "Reply with the tutor message only, following the system prompt's "
        "constraints."
    )
    return [
        {"role": "system", "content": TUTOR_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _coerce_verdict(raw: Any) -> GradeVerdict:
    """Pull a GradeVerdict out of whatever the provider returned.

    Anthropic's tool-use path stores a parsed instance under
    ``additional_kwargs['parsed']``. Local llama.cpp returns JSON in
    ``content`` — fall back to parsing that.
    """
    if hasattr(raw, "additional_kwargs"):
        parsed = raw.additional_kwargs.get("parsed") if raw.additional_kwargs else None
        if isinstance(parsed, GradeVerdict):
            return parsed
        if isinstance(parsed, dict):
            return GradeVerdict.model_validate(parsed)
        content = getattr(raw, "content", "") or ""
    else:
        content = str(raw)

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"grader response was not valid JSON: {content!r}") from e
    return GradeVerdict.model_validate(data)


# Module-level cancellation registry for the grading loop.
#
# When the submit endpoint times out waiting on the graph, the underlying
# thread keeps running (Python threads cannot be cancelled mid-call). The
# endpoint records ``(attempt_id, turn_index)`` here so the eventual
# ``_persist`` call sees the cancellation flag and skips writing — keeping
# the "do not consume an attempt" contract from §13 of the design doc.
_CANCELLED_TURNS: set[tuple[int, int]] = set()


def mark_turn_cancelled(attempt_id: int, turn_index: int) -> None:
    _CANCELLED_TURNS.add((attempt_id, turn_index))


def is_turn_cancelled(attempt_id: int, turn_index: int) -> bool:
    return (attempt_id, turn_index) in _CANCELLED_TURNS


def clear_turn_cancellation(attempt_id: int, turn_index: int) -> None:
    _CANCELLED_TURNS.discard((attempt_id, turn_index))


def _persist(
    engine: Engine,
    state: GraderState,
    *,
    user_id: int,
) -> tuple[int | None, str]:
    """Write the submission row, update attempt status, return (submission_id, status_after).

    If the submit endpoint has marked this turn cancelled (timeout), skip the
    write entirely and return (None, "cancelled").
    """
    attempt_id = state.attempt["id"]
    if is_turn_cancelled(attempt_id, state.turn_index):
        logger.info(
            "skipping persist for cancelled turn attempt=%s turn=%s",
            attempt_id,
            state.turn_index,
        )
        clear_turn_cancellation(attempt_id, state.turn_index)
        return None, "cancelled"
    question = state.question
    verdict = state.verdict
    if verdict is None:
        raise RuntimeError("persist called before grade produced a verdict")

    payload = state.submission_payload
    payload_kind = payload.get("kind", "text")
    payload_text = payload.get("text")
    artifact_path = payload.get("artifact_path")

    now = _now_iso()
    max_attempts = int(question.get("max_attempts") or 6)

    if verdict.verdict == "correct":
        next_status = "passed"
    elif state.turn_index >= max_attempts:
        next_status = "exhausted"
    else:
        next_status = "in_progress"

    with engine.begin() as conn:
        sub_id = conn.execute(
            text(
                "INSERT INTO submissions "
                "(attempt_id, turn_index, submitted_at, payload_kind, "
                " payload_text, artifact_path, grader_verdict, grader_score, "
                " grader_rationale, tutor_reply_md) "
                "VALUES (:aid, :ti, :sa, :pk, :pt, :ap, :gv, :gs, :gr, :tr)"
            ),
            {
                "aid": attempt_id,
                "ti": state.turn_index,
                "sa": now,
                "pk": payload_kind,
                "pt": payload_text,
                "ap": artifact_path,
                "gv": verdict.verdict,
                "gs": verdict.score,
                "gr": verdict.rationale,
                "tr": state.tutor_reply,
            },
        ).lastrowid

        if next_status == "passed":
            conn.execute(
                text(
                    "UPDATE attempts SET status='passed', completed_at=:c, "
                    "final_score=:s "
                    "WHERE id=:id"
                ),
                {"c": now, "s": verdict.score, "id": attempt_id},
            )
            conn.execute(
                text(
                    "INSERT INTO user_question_history "
                    "(user_id, assignment_id, category_id, question_id, "
                    " attempt_id, completed_at) "
                    "VALUES (:u, :a, :c, :q, :att, :ts)"
                ),
                {
                    "u": user_id,
                    "a": question["assignment_id"],
                    "c": question["category_id"],
                    "q": question["id"],
                    "att": attempt_id,
                    "ts": now,
                },
            )
        elif next_status == "exhausted":
            conn.execute(
                text(
                    "UPDATE attempts SET status='exhausted', completed_at=:c, "
                    "final_score=:s WHERE id=:id"
                ),
                {"c": now, "s": verdict.score, "id": attempt_id},
            )

    if next_status == "passed":
        mint_proof_token(attempt_id, engine=engine)

    return sub_id, next_status


def build_grader_graph(
    router: LLMRouter,
    engine: Engine,
    *,
    user_id: int,
) -> Callable[[GraderState], GraderState]:
    """Build and compile the five-node grading graph.

    Returns a callable wrapper that accepts a ``GraderState`` and returns the
    final state, dropping LangGraph's internal dict-based plumbing for callers.
    """

    def prepare(state: GraderState) -> dict[str, Any]:
        prior_turns = _load_prior_turns(engine, state.attempt["id"])
        return {"prior_turns": prior_turns}

    def preprocess(state: GraderState) -> dict[str, Any]:
        qtype = state.question.get("qtype", "text")
        if qtype != "image":
            return {}
        artifact_path = state.submission_payload.get("artifact_path")
        if not artifact_path:
            raise ValueError("image submission missing artifact_path")
        messages = build_vision_messages(
            question_prompt=state.question["prompt_md"],
            rubric=state.question["rubric_md"],
            image_path=artifact_path,
        )
        raw = router.invoke(
            "vision",
            messages,
            attempt_id=state.attempt["id"],
        )
        description = getattr(raw, "content", str(raw)) or ""
        return {"vision_description": description}

    def grade(state: GraderState) -> dict[str, Any]:
        qtype = state.question.get("qtype", "text")
        if qtype == "text":
            student_text = state.submission_payload.get("text", "")
            messages = build_grader_messages(
                question_prompt=state.question["prompt_md"],
                rubric=state.question["rubric_md"],
                student_text=student_text,
                prior_turns=state.prior_turns,
            )
            raw = router.invoke(
                "grader",
                messages,
                response_schema=GradeVerdict,
                attempt_id=state.attempt["id"],
            )
        elif qtype == "image":
            messages = build_grader_messages_from_vision(
                question_prompt=state.question["prompt_md"],
                rubric=state.question["rubric_md"],
                vision_description=state.vision_description or "",
                prior_turns=state.prior_turns,
            )
            raw = router.invoke(
                "grader",
                messages,
                response_schema=GradeVerdict,
                attempt_id=state.attempt["id"],
            )
        elif qtype == "python":
            artifact_path = state.submission_payload.get("artifact_path")
            student_source = state.submission_payload.get("text") or ""
            if not student_source and artifact_path:
                from pathlib import Path
                student_source = Path(artifact_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            messages = build_code_judge_messages(
                question_prompt=state.question["prompt_md"],
                rubric=state.question["rubric_md"],
                student_source=student_source,
                prior_turns=state.prior_turns,
            )
            raw = router.invoke(
                "code_judge",
                messages,
                response_schema=GradeVerdict,
                attempt_id=state.attempt["id"],
            )
        elif qtype == "excel":
            artifact_path = state.submission_payload.get("artifact_path")
            if not artifact_path:
                raise ValueError("excel submission missing artifact_path")
            messages = build_excel_grader_messages(
                question_prompt=state.question["prompt_md"],
                rubric=state.question["rubric_md"],
                prior_turns=state.prior_turns,
            )
            raw = router.invoke(
                "excel_grader",
                messages,
                response_schema=GradeVerdict,
                files=[artifact_path],
                attempt_id=state.attempt["id"],
            )
        else:
            raise ValueError(f"unsupported qtype: {qtype!r}")
        verdict = _coerce_verdict(raw)
        return {"verdict": verdict}

    def tutor(state: GraderState) -> dict[str, Any]:
        if state.verdict is None or state.verdict.verdict == "correct":
            return {"tutor_reply": None}
        qtype = state.question.get("qtype", "text")
        if qtype == "image":
            student_text = (
                "(student uploaded an image — vision summary below)\n"
                f"{state.vision_description or '(no description available)'}"
            )
        elif qtype == "python":
            student_text = state.submission_payload.get("text") or (
                "(student uploaded a Python source file)"
            )
        elif qtype == "excel":
            student_text = "(student uploaded an Excel workbook)"
        else:
            student_text = state.submission_payload.get("text", "")
        messages = _build_tutor_messages(
            question_prompt=state.question["prompt_md"],
            rubric=state.question["rubric_md"],
            student_text=student_text,
            verdict=state.verdict,
            prior_turns=state.prior_turns,
            attempt_index=state.turn_index,
        )
        raw = router.invoke(
            "tutor",
            messages,
            attempt_id=state.attempt["id"],
        )
        reply_text = getattr(raw, "content", str(raw)) or ""

        reference_tokens = extract_reference_tokens(
            state.question.get("reference_solution_md")
        )
        leaks = find_leaked_tokens(reply_text, reference_tokens)
        if leaks:
            logger.warning(
                "tutor leak detected for attempt %s turn %s: %s",
                state.attempt.get("id"),
                state.turn_index,
                sorted(leaks),
            )
            stricter = list(messages)
            stricter.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous reply leaked these tokens from the "
                        "reference solution: "
                        + ", ".join(sorted(leaks))
                        + ". Regenerate your reply WITHOUT mentioning any of "
                        "those tokens, numbers, variable names, or function "
                        "names. Stay Socratic and within the system prompt's "
                        "constraints."
                    ),
                }
            )
            retry = router.invoke(
                "tutor",
                stricter,
                attempt_id=state.attempt["id"],
            )
            retry_text = getattr(retry, "content", str(retry)) or ""
            retry_leaks = find_leaked_tokens(retry_text, reference_tokens)
            if retry_leaks:
                logger.error(
                    "tutor leak persisted after regeneration for attempt %s "
                    "turn %s: %s — using canned fallback",
                    state.attempt.get("id"),
                    state.turn_index,
                    sorted(retry_leaks),
                )
                return {"tutor_reply": _CANNED_TUTOR_REPLY}
            return {"tutor_reply": retry_text}
        return {"tutor_reply": reply_text}

    def persist(state: GraderState) -> dict[str, Any]:
        sub_id, status_after = _persist(engine, state, user_id=user_id)
        return {"submission_id": sub_id, "status_after": status_after}

    def _route_after_grade(state: GraderState) -> str:
        if state.verdict is not None and state.verdict.verdict == "correct":
            return "persist"
        return "tutor"

    g = StateGraph(GraderState)
    g.add_node("prepare", prepare)
    g.add_node("preprocess", preprocess)
    g.add_node("grade", grade)
    g.add_node("tutor", tutor)
    g.add_node("persist", persist)

    g.set_entry_point("prepare")
    g.add_edge("prepare", "preprocess")
    g.add_edge("preprocess", "grade")
    g.add_conditional_edges(
        "grade",
        _route_after_grade,
        {"tutor": "tutor", "persist": "persist"},
    )
    g.add_edge("tutor", "persist")
    g.add_edge("persist", END)

    compiled = g.compile()

    def run(state: GraderState) -> GraderState:
        out = compiled.invoke(state)
        if isinstance(out, GraderState):
            return out
        return GraderState.model_validate(out)

    return run


__all__ = [
    "GraderState",
    "build_grader_graph",
    "extract_reference_tokens",
    "find_leaked_tokens",
    "mark_turn_cancelled",
    "is_turn_cancelled",
    "clear_turn_cancellation",
]
