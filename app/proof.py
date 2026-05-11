"""Proof-of-work tokens — HMAC-SHA256 signed JWT-shaped strings.

Token shape per §15: ``base64url(payload).base64url(sig)`` where payload is
the canonical JSON of the attempt facts. Symmetric secret keeps the v1 verify
path trivial (no key distribution), and the format trivially upgrades to PyJWT
later if/when a third party needs to verify.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import settings
from app.db import get_engine


@dataclass
class ProofToken:
    """Result of minting: the token string + the inserted row id."""

    token: str
    payload: dict[str, Any]
    proof_token_id: int


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(payload_bytes: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()


def _build_token(payload: dict[str, Any], secret: str) -> tuple[str, str]:
    """Return (token, hmac_sig_b64url)."""
    payload_bytes = _canonical_json(payload)
    sig = _sign(payload_bytes, secret)
    payload_b64 = _b64url_encode(payload_bytes)
    sig_b64 = _b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}", sig_b64


def _answer_hash(attempt_id: int, engine: Engine) -> str:
    """SHA-256 over the canonical concatenation of all submission texts/paths.

    For text submissions we hash payload_text; for file uploads we hash the
    artifact path (the file itself is preserved on disk for audit). The goal
    is a tamper-evident summary that ties the token to *what was submitted*.
    """
    h = hashlib.sha256()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT turn_index, payload_kind, payload_text, artifact_path "
                "FROM submissions WHERE attempt_id = :a "
                "ORDER BY turn_index ASC"
            ),
            {"a": attempt_id},
        ).fetchall()
    for r in rows:
        chunk = (
            f"{r.turn_index}|{r.payload_kind}|"
            f"{r.payload_text or ''}|{r.artifact_path or ''}\n"
        ).encode("utf-8")
        h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _build_payload(attempt_id: int, engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT a.id              AS attempt_id, "
                "       a.user_id         AS user_id, "
                "       a.completed_at    AS completed_at, "
                "       a.final_score     AS final_score, "
                "       u.username        AS username, "
                "       q.qid             AS qid, "
                "       asg.slug          AS assignment_slug, "
                "       cat.name          AS category "
                "FROM attempts a "
                "JOIN users u       ON u.id = a.user_id "
                "JOIN questions q   ON q.id = a.question_id "
                "JOIN assignments asg ON asg.id = q.assignment_id "
                "JOIN categories cat  ON cat.id = q.category_id "
                "WHERE a.id = :a"
            ),
            {"a": attempt_id},
        ).fetchone()
        if row is None:
            raise LookupError(f"attempt {attempt_id} not found")
        sub_count = conn.execute(
            text("SELECT COUNT(*) FROM submissions WHERE attempt_id = :a"),
            {"a": attempt_id},
        ).scalar() or 0

    return {
        "user_id": row.user_id,
        "username": row.username,
        "assignment_slug": row.assignment_slug,
        "qid": row.qid,
        "category": row.category,
        "attempt_id": row.attempt_id,
        "completed_at": row.completed_at,
        "submission_count": int(sub_count),
        "final_score": row.final_score,
        "answer_hash": _answer_hash(attempt_id, engine),
    }


def mint(
    attempt_id: int,
    *,
    engine: Optional[Engine] = None,
    secret: Optional[str] = None,
) -> ProofToken:
    """Mint a proof token for a closed attempt.

    Inserts a row into ``proof_tokens`` and stamps ``attempts.proof_token_id``.
    Idempotent on the unique ``attempt_id`` index — if a row already exists,
    its existing token is returned.
    """
    eng = engine or get_engine()
    sec = secret if secret is not None else settings.HMAC_SECRET

    with eng.connect() as conn:
        existing = conn.execute(
            text(
                "SELECT id, payload_json, hmac_sig FROM proof_tokens "
                "WHERE attempt_id = :a"
            ),
            {"a": attempt_id},
        ).fetchone()
    if existing is not None:
        payload = json.loads(existing.payload_json)
        token_str = (
            f"{_b64url_encode(_canonical_json(payload))}.{existing.hmac_sig}"
        )
        return ProofToken(
            token=token_str, payload=payload, proof_token_id=existing.id
        )

    payload = _build_payload(attempt_id, eng)
    token_str, sig_b64 = _build_token(payload, sec)
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    issued_at = datetime.now(timezone.utc).isoformat()

    with eng.begin() as conn:
        proof_id = conn.execute(
            text(
                "INSERT INTO proof_tokens "
                "(attempt_id, payload_json, hmac_sig, issued_at) "
                "VALUES (:a, :p, :s, :i)"
            ),
            {"a": attempt_id, "p": payload_json, "s": sig_b64, "i": issued_at},
        ).lastrowid
        conn.execute(
            text("UPDATE attempts SET proof_token_id = :p WHERE id = :a"),
            {"p": proof_id, "a": attempt_id},
        )

    return ProofToken(token=token_str, payload=payload, proof_token_id=proof_id)


def verify(
    token_str: str, *, secret: Optional[str] = None
) -> tuple[bool, dict[str, Any]]:
    """Recompute the HMAC and return (ok, payload).

    Returns ``(False, {})`` on malformed input rather than raising — callers
    (CLI, admin views) want a uniform fail signal.
    """
    sec = secret if secret is not None else settings.HMAC_SECRET
    if not token_str or token_str.count(".") != 1:
        return False, {}
    payload_b64, sig_b64 = token_str.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_b64)
        provided_sig = _b64url_decode(sig_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return False, {}

    expected = _sign(_canonical_json(payload), sec)
    ok = hmac.compare_digest(provided_sig, expected)
    return ok, payload


def post_to_canvas(token: ProofToken) -> None:
    """No-op stub for the future Canvas API integration.

    The ``proof_tokens.canvas_posted_at`` column exists today so that wiring
    Canvas in becomes a one-function swap with no schema migration.
    """
    return None


__all__ = ["ProofToken", "mint", "verify", "post_to_canvas"]
