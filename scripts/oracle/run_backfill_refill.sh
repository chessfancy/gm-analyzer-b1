#!/usr/bin/env bash
set -euo pipefail
umask 077

REPO="/home/ubuntu/projects/gm-analyzer-b1"
CGM="/home/ubuntu/data/cgm"
mkdir -p "$CGM/locks" "$CGM/logs"
exec 9>"$CGM/locks/backfill-refill.lock"
/usr/bin/flock -n 9 || exit 0

cd "$REPO"
export PYTHONPATH=src
"$REPO/.venv/bin/python" scripts/oracle/refill_backfill_queue.py \
  >>"$CGM/logs/backfill-refill.log" 2>&1
