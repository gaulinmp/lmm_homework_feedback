from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python cli/load_assignments.py` from the project root.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.assignments_loader import load_assignment  # noqa: E402
from app.db import get_engine, init_db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load assignment markdown files into the tutor database."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=_ROOT / "assignments",
        help="Directory containing assignment .md files (default: ./assignments)",
    )
    args = parser.parse_args(argv)

    init_db()
    engine = get_engine()

    files = sorted(args.dir.glob("*.md"))
    if not files:
        print(f"no assignments found in {args.dir}")
        return 0

    counts = {"added": 0, "updated": 0, "skipped": 0}
    for f in files:
        status = load_assignment(engine, f)
        counts[status] = counts.get(status, 0) + 1
        print(f"{status:8s} {f.name}")
    print(
        f"summary: added={counts['added']} "
        f"updated={counts['updated']} skipped={counts['skipped']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
