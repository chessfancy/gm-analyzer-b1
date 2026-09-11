#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "=== ChessGrandmaster Analyzer setup ==="

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

echo
echo "=== Engine ==="

ENGINE_OK=0

if command -v stockfish >/dev/null 2>&1; then
    if cgm-engine verify "$(command -v stockfish)"; then
        ENGINE_OK=1
    else
        echo
        echo "Installed engine does not match engine.toml."
        echo "Installing configured engine..."
    fi
fi

if [[ "$ENGINE_OK" -ne 1 ]]; then
    ./scripts/install_stockfish.sh
fi

echo
echo "=== Verification ==="

cgm-engine verify /usr/local/bin/stockfish

python --version
cgm-analyze --help >/dev/null
pytest -q

echo
echo "Codespace ready."
