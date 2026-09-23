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

    if ! "$BOOTSTRAP_PYTHON" -m venv "$VENV"; then
        echo "Standard venv bootstrap failed; retrying without ensurepip."
        rm -rf "$VENV"

        "$BOOTSTRAP_PYTHON" -m venv --without-pip "$VENV"

        "$BOOTSTRAP_PYTHON" \
            -m pip \
            --python "$VENV/bin/python" \
            install \
            --upgrade pip
    fi
fi
PYTHON_BIN="$VENV/bin/python"
if [[ "${CGM_SKIP_PACKAGE_INSTALL:-0}" == "1" ]]; then
    echo "Reusing runtime Python dependencies."
    "$PYTHON_BIN" - <<'PY_RUNTIME'
import chess
print("python-chess:", chess.__version__)
PY_RUNTIME
else
    PACKAGE_SPEC="$ROOT"
    if [[ "${CGM_INSTALL_DEV:-1}" != "0" ]]; then
        PACKAGE_SPEC="${ROOT}[dev]"
    fi
    "$PYTHON_BIN" -m pip install -e "$PACKAGE_SPEC"
fi

run_cgm_engine() {
    if [[ -x "$VENV/bin/cgm-engine" ]]; then
        "$VENV/bin/cgm-engine" "$@"
    else
        PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}" \
            "$PYTHON_BIN" -m chessgrandmaster.engine_manifest "$@"
    fi
}

ENGINE_PATH="$(run_cgm_engine install-path)"
echo "Managed engine path: $ENGINE_PATH"
ENGINE_OK=0
if [[ -f "$ENGINE_PATH" ]] && run_cgm_engine verify "$ENGINE_PATH"; then ENGINE_OK=1; fi
if [[ "$ENGINE_OK" -ne 1 ]]; then CGM_PYTHON="$PYTHON_BIN" "$ROOT/scripts/install_stockfish.sh"; fi
run_cgm_engine verify "$ENGINE_PATH"
"$PYTHON_BIN" --version
echo "Environment ready."
echo "Venv   : $VENV"
echo "Engine : $ENGINE_PATH"