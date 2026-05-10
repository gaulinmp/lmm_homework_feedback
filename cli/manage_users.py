from __future__ import annotations

import argparse
import csv
import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import text  # noqa: E402

from app.auth import (  # noqa: E402
    DISABLED_PREFIX,
    LocalAuthBackend,
    _ph,
    revoke_all_sessions,
)
from app.db import get_engine, init_db  # noqa: E402


def _ensure_password(provided: str | None, prompt: str) -> str | None:
    if provided is not None:
        return provided
    if not sys.stdin.isatty():
        return None
    pw1 = getpass.getpass(prompt)
    if not pw1:
        return None
    pw2 = getpass.getpass("Confirm: ")
    if pw1 != pw2:
        print("passwords do not match", file=sys.stderr)
        return None
    return pw1


def cmd_add(args: argparse.Namespace) -> int:
    init_db()
    engine = get_engine()
    password = _ensure_password(args.password, "Password: ")
    if not password:
        print("password required", file=sys.stderr)
        return 2
    backend = LocalAuthBackend(engine)
    try:
        user = backend.create_user(
            args.username,
            password,
            role=args.role,
            canvas_user_id=args.canvas_user_id,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"added id={user.id} username={user.username} role={user.role}")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    init_db()
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, username, role, created_at, password_hash, canvas_user_id "
                "FROM users ORDER BY id"
            )
        ).fetchall()
    if not rows:
        print("(no users)")
        return 0
    print(f"{'id':>4}  {'username':20s}  {'role':8s}  {'status':8s}  created_at")
    for r in rows:
        status = "disabled" if r.password_hash.startswith(DISABLED_PREFIX) else "active"
        print(f"{r.id:>4}  {r.username:20s}  {r.role:8s}  {status:8s}  {r.created_at}")
    return 0


def _find_user_id(engine, username: str) -> int | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE username = :u"), {"u": username}
        ).fetchone()
    return row.id if row else None


def cmd_disable(args: argparse.Namespace) -> int:
    init_db()
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, password_hash FROM users WHERE username = :u"),
            {"u": args.username},
        ).fetchone()
        if row is None:
            print(f"no such user: {args.username}", file=sys.stderr)
            return 1
        if row.password_hash.startswith(DISABLED_PREFIX):
            print(f"{args.username} is already disabled")
            return 0
        conn.execute(
            text("UPDATE users SET password_hash = :p WHERE id = :i"),
            {"p": DISABLED_PREFIX + row.password_hash, "i": row.id},
        )
    revoke_all_sessions(engine, row.id)
    print(f"disabled {args.username} (sessions revoked)")
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    init_db()
    engine = get_engine()
    uid = _find_user_id(engine, args.username)
    if uid is None:
        print(f"no such user: {args.username}", file=sys.stderr)
        return 1
    password = _ensure_password(args.password, "New password: ")
    if not password:
        print("password required", file=sys.stderr)
        return 2
    new_hash = _ph.hash(password)
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET password_hash = :p WHERE id = :i"),
            {"p": new_hash, "i": uid},
        )
        conn.execute(
            text(
                "UPDATE sessions SET revoked_at = :n "
                "WHERE user_id = :i AND revoked_at IS NULL"
            ),
            {"n": now, "i": uid},
        )
    print(f"password reset for {args.username} (sessions revoked)")
    return 0


def cmd_import_csv(args: argparse.Namespace) -> int:
    init_db()
    engine = get_engine()
    backend = LocalAuthBackend(engine)
    added = skipped = 0
    with args.path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            username = (
                row.get("username")
                or row.get("user")
                or row.get("netid")
                or ""
            ).strip()
            if not username:
                skipped += 1
                continue
            password = (row.get("password") or args.default_password or "").strip()
            if not password:
                print(f"skip {username}: no password", file=sys.stderr)
                skipped += 1
                continue
            role = (row.get("role") or "student").strip() or "student"
            canvas_user_id = (row.get("canvas_user_id") or "").strip() or None
            try:
                backend.create_user(
                    username,
                    password,
                    role=role,
                    canvas_user_id=canvas_user_id,
                )
                added += 1
            except Exception as exc:
                print(f"skip {username}: {exc}", file=sys.stderr)
                skipped += 1
    print(f"imported: added={added} skipped={skipped}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage tutor users.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("add", help="add a new user")
    pa.add_argument("--username", required=True)
    pa.add_argument(
        "--role", default="student", choices=["student", "admin"]
    )
    pa.add_argument(
        "--password",
        default=None,
        help="password (prompts if omitted; required for non-tty)",
    )
    pa.add_argument("--canvas-user-id", default=None)
    pa.set_defaults(func=cmd_add)

    pl = sub.add_parser("list", help="list all users")
    pl.set_defaults(func=cmd_list)

    pd = sub.add_parser("disable", help="disable a user and revoke their sessions")
    pd.add_argument("--username", required=True)
    pd.set_defaults(func=cmd_disable)

    pr = sub.add_parser("reset-password", help="reset a user's password")
    pr.add_argument("--username", required=True)
    pr.add_argument("--password", default=None)
    pr.set_defaults(func=cmd_reset_password)

    pi = sub.add_parser("import-csv", help="bulk-import users from a CSV file")
    pi.add_argument("path", type=Path)
    pi.add_argument(
        "--default-password",
        default=None,
        help="password to use for rows without a password column",
    )
    pi.set_defaults(func=cmd_import_csv)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
