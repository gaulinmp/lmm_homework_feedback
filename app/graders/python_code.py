"""Build the message list for grading a python-source-type submission.

Hard rule (§8 + §14 of the design doc): we never execute student code. The
``code_judge`` role inspects source *statically* and decides whether it
plausibly implements what the question asked. The grader sees no output, no
stack traces — just the source itself, the prompt, and the rubric.
"""

from __future__ import annotations

from typing import Iterable

from app.graders.text import PriorTurn
from app.llm.prompts import CODE_JUDGE_SYSTEM_PROMPT


_MAX_SOURCE_CHARS = 60_000  # roughly 100KB cap from the upload validator


def _truncate(source: str) -> str:
    if len(source) <= _MAX_SOURCE_CHARS:
        return source
    head = source[: _MAX_SOURCE_CHARS // 2]
    tail = source[-_MAX_SOURCE_CHARS // 2 :]
    return f"{head}\n\n# ... [{len(source) - _MAX_SOURCE_CHARS} chars truncated] ...\n\n{tail}"


def build_code_judge_messages(
    *,
    question_prompt: str,
    rubric: str,
    student_source: str,
    prior_turns: Iterable[PriorTurn] = (),
) -> list[dict[str, str]]:
    """Return messages for the code_judge role asking it to grade the source."""
    history_lines: list[str] = []
    for t in prior_turns:
        history_lines.append(
            f"Turn {t.turn_index} — verdict: {t.grader_verdict}\n"
            f"Grader rationale: {t.grader_rationale}"
        )
    history = "\n\n---\n\n".join(history_lines) if history_lines else (
        "(no prior turns — this is the first submission)"
    )

    safe_source = _truncate(student_source)

    user_content = (
        "Question prompt:\n"
        f"{question_prompt.strip()}\n\n"
        "Rubric:\n"
        f"{rubric.strip()}\n\n"
        "Prior turns in this attempt:\n"
        f"{history}\n\n"
        "You are reviewing the STATIC SOURCE CODE below. The code has NOT "
        "been executed; you cannot see its output. Judge whether the source "
        "plausibly implements what the question asks against the rubric.\n\n"
        "Student's submitted Python source:\n"
        "```python\n"
        f"{safe_source.strip()}\n"
        "```\n\n"
        "Return the GradeVerdict JSON object. Do not address the student."
    )

    return [
        {"role": "system", "content": CODE_JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


__all__ = ["build_code_judge_messages"]
