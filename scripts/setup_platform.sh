#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
BOOTSTRAP_PYTHON="${CGM_BOOTSTRAP_PYTHON:-}"
if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
    if command -v python3 >/dev/null 2>&1; then BOOTSTRAP_PYTHON="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then BOOTSTRAP_PYTHON="$(command -v python)"
    else echo "Python 3 was not found." >&2; exit 1; fi
fi
"$BOOTSTRAP_PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"ChessGrandmaster requires Python 3.11+. Current: {sys.version.split()[0]}")
print("Bootstrap Python:", sys.version.split()[0])
PY
VENV="${CGM_VENV:-$ROOT/.venv}"
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "Creating virtual environment: $VENV"
    "$BOOTSTRAP_PYTHON" -m venv "$VENV"
fi
PYTHON_BIN="$VENV/bin/python"
CGM_ENGINE="$VENV/bin/cgm-engine"
"$PYTHON_BIN" -m pip install -e "${ROOT}[dev]"
ENGINE_PATH="$("$CGM_ENGINE" install-path)"
echo "Managed engine path: $ENGINE_PATH"
ENGINE_OK=0
if [[ -f "$ENGINE_PATH" ]] && "$CGM_ENGINE" verify "$ENGINE_PATH"; then ENGINE_OK=1; fi
if [[ "$ENGINE_OK" -ne 1 ]]; then CGM_PYTHON="$PYTHON_BIN" "$ROOT/scripts/install_stockfish.sh"; fi
"$CGM_ENGINE" verify "$ENGINE_PATH"
"$PYTHON_BIN" --version
echo "Environment ready."
echo "Venv   : $VENV"
echo "Engine : $ENGINE_PATH"
