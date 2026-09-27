#!/usr/bin/env bash
set -euo pipefail

REPO=/home/ubuntu/projects/gm-analyzer-b1
CGM=/home/ubuntu/data/cgm
LOCK="$CGM/locks/deepnote-manual-bridge.lock"
LOG="$CGM/logs/deepnote-manual-bridge.log"
mkdir -p "$CGM/locks" "$CGM/logs"

exec 9>"$LOCK"
flock -n 9 || exit 0
cd "$REPO"
PYTHONPATH=src .venv/bin/python scripts/oracle/deepnote_manual_bridge.py >>"$LOG" 2>&1
