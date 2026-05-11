#!/usr/bin/env bash
# Start the FastAPI app under uvicorn with 2 workers.
#
# § Key decision: two workers, not one. LLM calls are async-bound, so two
# workers gives free failover if one dies. Workers share the SQLite DB via
# WAL mode; that's enabled in app.db.make_engine.
#
# This script is invoked by deploy/tutor-app.service. Bind address is loopback
# only — caddy terminates TLS and reverse-proxies (see deploy/caddyfile).

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$APP_DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"
WORKERS="${WORKERS:-2}"

# Make structured logging the default in service mode (override with LOG_JSON=0).
export LOG_JSON="${LOG_JSON:-1}"
export ENV="${ENV:-prod}"

exec uv run uvicorn app.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --workers "$WORKERS" \
    --proxy-headers \
    --forwarded-allow-ips "127.0.0.1" \
    --no-access-log
