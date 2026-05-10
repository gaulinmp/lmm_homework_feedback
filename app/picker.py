from __future__ import annotations

import random
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine, Row


def pick_next_question(
    engine: Engine,
    user_id: int,
    assignment_id: int,
    *,
    rng: Optional[random.Random] = None,
) -> Row:
    """Pick the next question for (user, assignment).

    Categories not yet present in `user_question_history` for this user are
    preferred; within the candidate pool, selection is uniform random via `rng`.
    Once every category has been completed at least once, fall back to picking
    from all questions (repeats allowed).
    """
    rng = rng if rng is not None else random.Random()

    with engine.connect() as conn:
        seen_rows = conn.execute(
            text(
                """
                SELECT DISTINCT category_id
                FROM user_question_history
                WHERE user_id = :u AND assignment_id = :a
                """
            ),
            {"u": user_id, "a": assignment_id},
        ).fetchall()
        seen_categories = {r.category_id for r in seen_rows}

        all_questions = conn.execute(
            text(
                """
                SELECT id, assignment_id, category_id, qid, qtype, prompt_md,
                       rubric_md, reference_solution_md, data_files_json,
                       max_attempts, metadata_json
                FROM questions
                WHERE assignment_id = :a
                ORDER BY id
                """
            ),
            {"a": assignment_id},
        ).fetchall()

    if not all_questions:
        raise LookupError(f"no questions found for assignment_id={assignment_id}")

    unseen = [q for q in all_questions if q.category_id not in seen_categories]
    pool = unseen if unseen else all_questions
    return rng.choice(pool)
