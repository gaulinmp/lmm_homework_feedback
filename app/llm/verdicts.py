from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GradeVerdict(BaseModel):
    verdict: Literal["correct", "partial", "incorrect", "error"]
    score: float = Field(ge=0.0, le=1.0)
    rationale: str
    weakest_concept: str | None = None
