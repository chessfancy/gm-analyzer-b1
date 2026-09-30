#!/usr/bin/env bash
set -euo pipefail
umask 077
REPO=/home/ubuntu/projects/gm-analyzer-b1
CGM=/home/ubuntu/data/cgm
SECRET=/home/ubuntu/.config/cgm/secrets/molab_http.env
mkdir -p "$CGM/locks" "$CGM/logs"
[[ -f "$SECRET" ]] || exit 0
exec 9>"$CGM/locks/molab-http-bridge.lock"
flock -n 9 || exit 0
set -a; source "$SECRET"; set +a
cd "$REPO"
PYTHONPATH=src .venv/bin/python scripts/oracle/run_molab_http_bridge.py >>"$CGM/logs/molab-http-bridge.log" 2>&1
