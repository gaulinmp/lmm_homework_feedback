#!/usr/bin/env bash
# Snapshot the SQLite DB into the Dropbox backup folder.
#
# Uses sqlite3 `.backup` so a running uvicorn isn't disrupted (vs. cp, which
# can capture a torn WAL). Retains the last $RETAIN snapshots; older ones
# are deleted.
#
# Cron line (also documented in deploy/README.md):
#   15 2 * * *  /home/$USER/llm_homework_tutor/scripts/backup.sh >/dev/null 2>&1

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DB_PATH="${DB_PATH:-$APP_DIR/data/tutor.db}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/Dropbox/llm_homework_tutor_backups}"
RETAIN="${RETAIN:-30}"

mkdir -p "$BACKUP_DIR"

stamp="$(date +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/tutor-$stamp.db"

if [ ! -f "$DB_PATH" ]; then
    echo "backup.sh: source DB not found at $DB_PATH" >&2
    exit 1
fi

sqlite3 "$DB_PATH" ".backup '$out'"

if [ ! -s "$out" ]; then
    echo "backup.sh: empty backup file produced at $out" >&2
    exit 2
fi

# Prune anything older than the most recent $RETAIN snapshots.
# Sort by mtime desc, skip the first $RETAIN, delete the rest.
mapfile -t old < <(ls -t "$BACKUP_DIR"/tutor-*.db 2>/dev/null | tail -n +$((RETAIN + 1)))
for f in "${old[@]:-}"; do
    [ -n "$f" ] && rm -f -- "$f"
done

echo "$out"
