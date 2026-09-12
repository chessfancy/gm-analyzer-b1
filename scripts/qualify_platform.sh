#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/setup_platform.sh"
VENV="${CGM_VENV:-$ROOT/.venv}"
ENGINE_PATH="$("$VENV/bin/cgm-engine" install-path)"
CGM_RUNTIME_HOME="${CGM_HOME:-$ROOT/.cgm}"
REPORT_DIR="${CGM_BENCH_REPORT_DIR:-$CGM_RUNTIME_HOME/benchmarks}"
mkdir -p "$REPORT_DIR"
HOST="$(hostname 2>/dev/null || printf 'unknown')"
HOST="$(printf '%s' "$HOST" | tr -c 'A-Za-z0-9._-' '_')"
STAMP="$(date -u '+%Y%m%dT%H%M%SZ')"
REPORT="${CGM_BENCH_JSON_OUT:-$REPORT_DIR/qualification_${HOST}_${STAMP}.json}"
"$VENV/bin/cgm-bench" qualify \
    --pgn "$ROOT/tests/golden/tre_2026_game_1.pgn" \
    --engine "$ENGINE_PATH" \
    --samples "${CGM_BENCH_SAMPLES:-12}" \
    --fixed-nodes "${CGM_BENCH_FIXED_NODES:-1000000}" \
    --depths "${CGM_BENCH_DEPTHS:-18,19,20}" \
    --hash-mb "${CGM_BENCH_HASH_MB:-256}" \
    --target-p95 "${CGM_BENCH_TARGET_P95:-3.0}" \
    --tournament-moves "${CGM_BENCH_TOURNAMENT_MOVES:-14000}" \
    "$@" \
    --json-out "$REPORT"
echo "QUALIFICATION COMPLETE: $REPORT"
