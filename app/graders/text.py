"""Build the message list for grading a text-type submission.

The router will call ``LLMRouter.invoke("grader", messages, response_schema=GradeVerdict)``
on the result, so the messages encode the question, rubric, the student's
latest text, and a compact transcript of prior turns within this attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.llm.prompts import GRADER_SYSTEM_PROMPT


@dataclass
class PriorTurn:
    """Compact record of a previous attempt turn for the grader's context."""

    turn_index: int
    student_text: str
    grader_verdict: str
    grader_rationale: str


def _format_prior_turns(prior_turns: Iterable[PriorTurn]) -> str:
    lines: list[str] = []
    for t in prior_turns:
        lines.append(
            f"Turn {t.turn_index} — verdict: {t.grader_verdict}\n"
            f"Student wrote:\n{t.student_text}\n"
            f"Grader rationale: {t.grader_rationale}"
        )
    if not lines:
        return "(no prior turns — this is the first submission)"
    return "\n\n---\n\n".join(lines)


def build_grader_messages(
    *,
    question_prompt: str,
    rubric: str,
    student_text: str,
    prior_turns: Iterable[PriorTurn] = (),
) -> list[dict[str, str]]:
    """Return the message list to pass to ``LLMRouter.invoke('grader', ...)``."""
    transcript = _format_prior_turns(prior_turns)
    user_content = (
        "Question prompt:\n"
        f"{question_prompt.strip()}\n\n"
        "Rubric:\n"
        f"{rubric.strip()}\n\n"
        "Prior turns in this attempt:\n"
        f"{transcript}\n\n"
        "Student's latest submission:\n"
        f"{student_text.strip()}\n\n"
        "Grade the latest submission against the rubric and return the "
        "GradeVerdict JSON object. Reminder: do not address the student."
    )
    return [
        {"role": "system", "content": GRADER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


__all__ = ["PriorTurn", "build_grader_messages"]
