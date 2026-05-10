"""Build message lists for grading an image-type submission.

The image qtype uses two LLM hops:

1. ``preprocess`` — call ``LLMRouter.invoke("vision", ...)`` with the image to
   produce a structured *description* of what's visible in the chart and an
   initial assessment against each rubric bullet. The vision role sees pixels
   the downstream grader can't.
2. ``grade`` — call ``LLMRouter.invoke("grader", ..., response_schema=GradeVerdict)``
   with the rubric + the vision description as the "student submission". The
   grader emits the structured verdict the rest of the pipeline expects.

The split keeps the verdict schema identical across qtypes; only the upstream
preprocess step is image-aware.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Iterable

from app.graders.text import PriorTurn
from app.llm.prompts import GRADER_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT


_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _mime_for(path: Path) -> str:
    return _IMAGE_MIME.get(path.suffix.lower(), "application/octet-stream")


def build_vision_messages(
    *,
    question_prompt: str,
    rubric: str,
    image_path: str | Path,
) -> list[dict]:
    """Return messages for the vision role asking it to describe + judge."""
    path = Path(image_path)
    image_bytes = path.read_bytes()
    encoded = base64.b64encode(image_bytes).decode("ascii")
    mime = _mime_for(path)

    user_content: list[dict] = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        },
        {
            "type": "text",
            "text": (
                "Question prompt:\n"
                f"{question_prompt.strip()}\n\n"
                "Rubric:\n"
                f"{rubric.strip()}\n\n"
                "Describe what you see in this chart, then for each rubric "
                "bullet say whether the chart appears to satisfy it. Be "
                "concrete: name the axis labels, units, legend entries, and "
                "any obvious visual choices the student made. Do not address "
                "the student — your output will be read by a downstream grader."
            ),
        },
    ]

    return [
        {"role": "system", "content": VISION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def build_grader_messages_from_vision(
    *,
    question_prompt: str,
    rubric: str,
    vision_description: str,
    prior_turns: Iterable[PriorTurn] = (),
) -> list[dict[str, str]]:
    """Return grader messages that consume the vision description as evidence."""
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
        "The student uploaded an image. A vision model produced the following "
        "structured description of what is actually visible in the image:\n\n"
        f"{vision_description.strip()}\n\n"
        "Treat that description as ground truth about what the chart shows. "
        "Grade against the rubric and return the GradeVerdict JSON object. "
        "Do not address the student."
    )

    return [
        {"role": "system", "content": GRADER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


__all__ = [
    "build_vision_messages",
    "build_grader_messages_from_vision",
]
