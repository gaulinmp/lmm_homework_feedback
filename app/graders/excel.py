"""Build the message list for grading an Excel-workbook submission.

Excel grading routes to Anthropic's Claude Excel skill: the ``.xlsx`` is
uploaded to Anthropic via the Files API and attached to the message; the model
reasons over the workbook directly. In v1 this role is pinned to Anthropic in
``config/llm.toml``; an all-local fallback (openpyxl) is punted to todo.md.
"""

from __future__ import annotations

from typing import Iterable

from app.graders.text import PriorTurn
from app.llm.prompts import EXCEL_GRADER_SYSTEM_PROMPT


def build_excel_grader_messages(
    *,
    question_prompt: str,
    rubric: str,
    prior_turns: Iterable[PriorTurn] = (),
) -> list[dict[str, str]]:
    """Return messages for the excel_grader role. File is passed via ``files=``."""
    history_lines: list[str] = []
    for t in prior_turns:
        history_lines.append(
            f"Turn {t.turn_index} — verdict: {t.grader_verdict}\n"
            f"Grader rationale: {t.grader_rationale}"
        )
    history = "\n\n---\n\n".join(history_lines) if history_lines else (
        "(no prior turns — this is the first submission)"
    )

    user_content = (
        "Question prompt:\n"
        f"{question_prompt.strip()}\n\n"
        "Rubric:\n"
        f"{rubric.strip()}\n\n"
        "Prior turns in this attempt:\n"
        f"{history}\n\n"
        "The student has uploaded an Excel workbook (attached). Open it, "
        "inspect the cells, formulas, named ranges, and any pivot or chart "
        "objects. Grade the workbook against the rubric and return the "
        "GradeVerdict JSON object. Do not address the student."
    )

    return [
        {"role": "system", "content": EXCEL_GRADER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


__all__ = ["build_excel_grader_messages"]
