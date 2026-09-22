#!/usr/bin/env bash
set -euo pipefail
umask 077

LOCK="/home/ubuntu/data/cgm/locks/lichess-olympiad.lock"
LOG="/home/ubuntu/data/cgm/logs/lichess-olympiad.log"
REPO="/home/ubuntu/projects/gm-analyzer-b1"

mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")"
exec 9>"$LOCK"
/usr/bin/flock -n 9 || exit 0

cd "$REPO"
printf '%s start lichess olympiad refresh\n' "$(date -Is)" >> "$LOG"
if PYTHONPATH=src .venv/bin/python scripts/oracle/refresh_lichess_olympiad.py >> "$LOG" 2>&1; then
  printf '%s success\n' "$(date -Is)" >> "$LOG"
else
  rc=$?
  printf '%s failed rc=%s\n' "$(date -Is)" "$rc" >> "$LOG"
  exit "$rc"
fi
