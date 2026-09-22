#!/usr/bin/env bash
set -euo pipefail
umask 077

LOCK="/home/ubuntu/data/cgm/locks/chess-results.lock"
LOG="/home/ubuntu/data/cgm/logs/chess-results.log"
REPORTS="/home/ubuntu/data/cgm/reports"
REPO="/home/ubuntu/projects/gm-analyzer-b1"
ROOT="/home/ubuntu/data/cgm/corpus-2026"
REGISTRY="$ROOT/registry.sqlite"

mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")" "$REPORTS"
exec 9>"$LOCK"
/usr/bin/flock -n 9 || exit 0

cd "$REPO"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FETCH_REPORT="$REPORTS/chess-results-fetch-$STAMP.json"
PROCESS_REPORT="$REPORTS/chess-results-process-$STAMP.json"

printf '%s start chess-results refresh\n' "$(date -Is)" >> "$LOG"
if .venv/bin/cgm-fetch chess-results corpus \
  --year 2026 \
  --max-lines 2000 \
  --refresh-recent-days 14 \
  --timeout 30 \
  --registry "$REGISTRY" \
  --root "$ROOT" \
  --report "$FETCH_REPORT" >> "$LOG" 2>&1
then
  printf '%s fetch success\n' "$(date -Is)" >> "$LOG"
else
  rc=$?
  printf '%s fetch failed rc=%s; processor not started\n' "$(date -Is)" "$rc" >> "$LOG"
  exit "$rc"
fi

if .venv/bin/cgm-process \
  --registry "$REGISTRY" \
  --root "$ROOT" \
  --workers auto \
  --game-timeout-sec 2.0 \
  --report "$PROCESS_REPORT" >> "$LOG" 2>&1
then
  printf '%s process success\n' "$(date -Is)" >> "$LOG"
else
  rc=$?
  printf '%s process failed rc=%s\n' "$(date -Is)" "$rc" >> "$LOG"
  exit "$rc"
fi
