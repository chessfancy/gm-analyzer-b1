#!/usr/bin/env bash
set -euo pipefail
umask 077
REPO="/home/ubuntu/projects/gm-analyzer-b1"
SECRETS="/home/ubuntu/.config/cgm/secrets/molab_s3.env"
LOCK="/home/ubuntu/data/cgm/locks/molab-s3-bridge.lock"
LOG="/home/ubuntu/data/cgm/logs/molab-s3-bridge.log"
[[ -f "$SECRETS" ]] || exit 0
mkdir -p "$(dirname "$LOCK")" "$(dirname "$LOG")"
exec 9>"$LOCK"
/usr/bin/flock -n 9 || exit 0
set -a
# shellcheck disable=SC1090
source "$SECRETS"
set +a
cd "$REPO"
printf '%s start\n' "$(date -Is)" >> "$LOG"
PYTHONPATH=src .venv/bin/python scripts/oracle/run_molab_s3_bridge.py >> "$LOG" 2>&1
printf '%s success\n' "$(date -Is)" >> "$LOG"
