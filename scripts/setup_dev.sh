#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

echo "=== ChessGrandmaster Analyzer setup ==="

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

if command -v stockfish >/dev/null 2>&1; then
    echo "Stockfish already installed:"
    stockfish <<'UCI' | grep -m1 '^id name'
uci
quit
UCI
else
    ./scripts/install_stockfish.sh
fi

echo
echo "=== Verification ==="

python --version
cgm-analyze --help >/dev/null
pytest -q

echo
echo "Codespace ready."
