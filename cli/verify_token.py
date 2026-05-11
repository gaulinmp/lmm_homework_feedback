"""Recompute a proof token's HMAC and print the parsed payload.

Usage::

    python cli/verify_token.py <token>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.proof import verify  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an HMAC proof-of-work token."
    )
    parser.add_argument("token", help="the token string (payload.sig)")
    args = parser.parse_args(argv)

    ok, payload = verify(args.token)
    status = "OK" if ok else "FAIL"
    print(status)
    if payload:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
