from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.engine import Engine

VALID_QTYPES = {"text", "image", "python", "excel"}

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_QUESTION_SPLIT_RE = re.compile(r"^## Question[ \t]+", re.MULTILINE)
_YAML_FENCE_RE = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)

_KNOWN_QUESTION_KEYS = {
    "qid",
    "type",
    "category",
    "rubric",
    "reference_solution",
    "data_files",
    "max_attempts",
}


def _parse_markdown(path: Path, raw: str) -> tuple[dict[str, Any], str, list[dict[str, Any]]]:
    """Return (frontmatter, body_after_frontmatter, [{config, prompt_md}, ...])."""
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ValueError(f"{path}: missing or malformed YAML frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    if not isinstance(fm, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    body = m.group(2)

    parts = _QUESTION_SPLIT_RE.split(body)
    questions: list[dict[str, Any]] = []
    for chunk in parts[1:]:
        rest = chunk.split("\n", 1)[1] if "\n" in chunk else ""
        ym = _YAML_FENCE_RE.search(rest)
        if not ym:
            raise ValueError(f"{path}: question block missing ```yaml fence")
        cfg = yaml.safe_load(ym.group(1)) or {}
        if not isinstance(cfg, dict):
            raise ValueError(f"{path}: question yaml must be a mapping")
        prompt_md = rest[ym.end():].strip()
        questions.append({"config": cfg, "prompt_md": prompt_md})

    return fm, body, questions


def _declared_categories(path: Path, fm: dict[str, Any]) -> list[tuple[str, int]]:
    cat_list = fm.get("categories") or []
    if not isinstance(cat_list, list):
        raise ValueError(f"{path}: 'categories' must be a list")
    out: list[tuple[str, int]] = []
    for i, c in enumerate(cat_list):
        if isinstance(c, dict):
            name = c.get("name")
        else:
            name = c
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: malformed category entry: {c!r}")
        out.append((name, i))
    return out


def load_assignment(engine: Engine, path: Path) -> str:
    """Load a single assignment markdown file. Returns 'added', 'updated', or 'skipped'."""
    raw = path.read_text(encoding="utf-8")
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    fm, body_md, questions = _parse_markdown(path, raw)

    slug = fm.get("slug")
    title = fm.get("title")
    if not slug:
        raise ValueError(f"{path}: frontmatter missing 'slug'")
    if not title:
        raise ValueError(f"{path}: frontmatter missing 'title'")
    week = fm.get("week")
    max_credit_questions = int(fm.get("max_credit_questions", 1))

    declared_cats = _declared_categories(path, fm)
    declared_names = {n for n, _ in declared_cats}

    seen_qids: set[str] = set()
    for q in questions:
        cfg = q["config"]
        qid = cfg.get("qid")
        qtype = cfg.get("type")
        cat = cfg.get("category")
        if not qid:
            raise ValueError(f"{path}: question missing 'qid'")
        if qid in seen_qids:
            raise ValueError(f"{path}: duplicate qid {qid!r}")
        seen_qids.add(qid)
        if qtype not in VALID_QTYPES:
            raise ValueError(
                f"{path}: question {qid!r} has invalid qtype {qtype!r}; "
                f"expected one of {sorted(VALID_QTYPES)}"
            )
        if cat not in declared_names:
            raise ValueError(
                f"{path}: question {qid!r} references undeclared category {cat!r}"
            )

    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id, content_hash FROM assignments WHERE slug = :slug"),
            {"slug": slug},
        ).fetchone()
        if existing is not None and existing.content_hash == content_hash:
            return "skipped"

        now = datetime.now(timezone.utc).isoformat()
        frontmatter_json = json.dumps(fm, default=str, sort_keys=True)

        if existing is None:
            result = conn.execute(
                text(
                    """
                    INSERT INTO assignments
                        (slug, week, title, source_path, frontmatter_json,
                         body_md, content_hash, max_credit_questions, loaded_at)
                    VALUES
                        (:slug, :week, :title, :source_path, :fm,
                         :body, :hash, :mcq, :loaded_at)
                    """
                ),
                {
                    "slug": slug, "week": week, "title": title,
                    "source_path": str(path), "fm": frontmatter_json,
                    "body": body_md, "hash": content_hash,
                    "mcq": max_credit_questions, "loaded_at": now,
                },
            )
            assignment_id = result.lastrowid
            status = "added"
        else:
            assignment_id = existing.id
            conn.execute(
                text(
                    """
                    UPDATE assignments SET
                        week=:week, title=:title, source_path=:source_path,
                        frontmatter_json=:fm, body_md=:body, content_hash=:hash,
                        max_credit_questions=:mcq, loaded_at=:loaded_at
                    WHERE id=:id
                    """
                ),
                {
                    "id": assignment_id, "week": week, "title": title,
                    "source_path": str(path), "fm": frontmatter_json,
                    "body": body_md, "hash": content_hash,
                    "mcq": max_credit_questions, "loaded_at": now,
                },
            )
            status = "updated"

        cat_id_by_name: dict[str, int] = {}
        for name, idx in declared_cats:
            row = conn.execute(
                text("SELECT id FROM categories WHERE assignment_id=:a AND name=:n"),
                {"a": assignment_id, "n": name},
            ).fetchone()
            if row is None:
                cat_id = conn.execute(
                    text(
                        "INSERT INTO categories (assignment_id, name, ordering_index) "
                        "VALUES (:a, :n, :o)"
                    ),
                    {"a": assignment_id, "n": name, "o": idx},
                ).lastrowid
            else:
                cat_id = row.id
                conn.execute(
                    text("UPDATE categories SET ordering_index=:o WHERE id=:id"),
                    {"o": idx, "id": cat_id},
                )
            cat_id_by_name[name] = cat_id

        for q in questions:
            cfg = q["config"]
            qid = cfg["qid"]
            qtype = cfg["type"]
            cat_id = cat_id_by_name[cfg["category"]]
            rubric = cfg.get("rubric") or ""
            ref_solution = cfg.get("reference_solution")
            data_files = cfg.get("data_files") or []
            max_attempts = int(cfg.get("max_attempts", 6))
            metadata = {k: v for k, v in cfg.items() if k not in _KNOWN_QUESTION_KEYS}
            params = {
                "a": assignment_id, "c": cat_id, "qid": qid, "qtype": qtype,
                "prompt": q["prompt_md"], "rubric": rubric, "ref": ref_solution,
                "data_files": json.dumps(data_files),
                "max_attempts": max_attempts,
                "metadata": json.dumps(metadata, default=str, sort_keys=True),
            }
            row = conn.execute(
                text("SELECT id FROM questions WHERE assignment_id=:a AND qid=:qid"),
                {"a": assignment_id, "qid": qid},
            ).fetchone()
            if row is None:
                conn.execute(
                    text(
                        """
                        INSERT INTO questions
                            (assignment_id, category_id, qid, qtype, prompt_md, rubric_md,
                             reference_solution_md, data_files_json, max_attempts, metadata_json)
                        VALUES
                            (:a, :c, :qid, :qtype, :prompt, :rubric, :ref,
                             :data_files, :max_attempts, :metadata)
                        """
                    ),
                    params,
                )
            else:
                conn.execute(
                    text(
                        """
                        UPDATE questions SET
                            category_id=:c, qtype=:qtype, prompt_md=:prompt, rubric_md=:rubric,
                            reference_solution_md=:ref, data_files_json=:data_files,
                            max_attempts=:max_attempts, metadata_json=:metadata
                        WHERE assignment_id=:a AND qid=:qid
                        """
                    ),
                    params,
                )

        return status
