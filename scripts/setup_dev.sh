#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/setup_platform.sh"
VENV="${CGM_VENV:-$ROOT/.venv}"
"$VENV/bin/cgm-analyze" --help >/dev/null
"$VENV/bin/cgm-bench" --help >/dev/null
"$VENV/bin/pytest" -q
echo "Development environment ready."
