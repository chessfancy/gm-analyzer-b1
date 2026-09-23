#!/usr/bin/env bash
set -euo pipefail
umask 077

PROVIDER="${1:?provider required}"
SLOT="${2:?slot required}"
REPO="/home/ubuntu/projects/gm-analyzer-b1"
CGM="/home/ubuntu/data/cgm"
PYTHON="$REPO/.venv/bin/python"
MIN_PRIORITY=500

mkdir -p "$CGM/locks" "$CGM/logs"
LOCK="$CGM/locks/olympiad-${PROVIDER}-${SLOT}.lock"
LOG="$CGM/logs/olympiad-${PROVIDER}-${SLOT}.log"
exec 9>"$LOCK"
/usr/bin/flock -n 9 || exit 0

cd "$REPO"
export PYTHONPATH=src

case "$PROVIDER" in
  kaggle)
    CMD=(
      "$PYTHON" scripts/oracle/kaggle_dispatch.py
      --min-priority "$MIN_PRIORITY"
      --poll-seconds 30
      --timeout-seconds 14400
    )
    ;;
  deepnote)
    CMD=(
      "$PYTHON" scripts/oracle/deepnote_dispatch.py
      --min-priority "$MIN_PRIORITY"
      --max-plies 2000
      --poll-seconds 30
      --timeout-seconds 4000
    )
    ;;
  codespaces)
    CMD=(
      "$PYTHON" scripts/oracle/codespaces_dispatch.py
      --min-priority "$MIN_PRIORITY"
      --poll-seconds 5
      --timeout-seconds 14400
      --keep-running
    )
    ;;
  *)
    echo "unknown provider: $PROVIDER" >&2
    exit 2
    ;;
esac

{
  printf '%s start provider=%s slot=%s\n' "$(date -Is)" "$PROVIDER" "$SLOT"
  if "${CMD[@]}"; then
    printf '%s success provider=%s slot=%s\n' "$(date -Is)" "$PROVIDER" "$SLOT"
  else
    rc=$?
    printf '%s failed provider=%s slot=%s rc=%s\n' "$(date -Is)" "$PROVIDER" "$SLOT" "$rc"
    exit "$rc"
  fi
} >>"$LOG" 2>&1
