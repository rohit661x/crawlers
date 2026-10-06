#!/usr/bin/env bash
# Daily consistent snapshot of the careers poller's SQLite state to Spaces. One file per
# weekday (careers-Mon.db.gz ...), so the last 7 days are kept and old ones overwrite themselves.
set -euo pipefail
db="${CAREERS_DB:-$HOME/data/state/careers.db}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
snap="$tmp/careers-$(date +%a).db"
# take the poller's run lock so the snapshot never lands mid-run
flock -w 900 "$db.lock" sqlite3 "$db" ".backup '$snap'"
[[ "$(sqlite3 "$snap" 'PRAGMA integrity_check')" == ok ]] || { echo "integrity check failed" >&2; exit 1; }
gzip -9 "$snap"
rclone copy "$snap.gz" "spaces:rohit-base/state" --log-level NOTICE
echo "backed up $(du -h "$snap.gz" | cut -f1) to spaces:rohit-base/state/$(basename "$snap.gz")"
