#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${CGM_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    if command -v python3 >/dev/null 2>&1; then PYTHON_BIN="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then PYTHON_BIN="$(command -v python)"
    else echo "Python 3 was not found." >&2; exit 1; fi
fi
for command_name in curl tar sha256sum install find; do
    command -v "$command_name" >/dev/null 2>&1 || { echo "Required command not found: $command_name" >&2; exit 1; }
done
eval "$(PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m chessgrandmaster.engine_manifest shell-install-spec)"
INSTALL_PATH="$(PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m chessgrandmaster.engine_manifest install-path)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Installing ${CGM_ENGINE_LABEL}"
echo "Asset        : ${CGM_ENGINE_ASSET}"
echo "Download     : ${CGM_ENGINE_URL}"
echo "Install path : ${INSTALL_PATH}"
curl -fL --retry 3 --retry-delay 2 "$CGM_ENGINE_URL" -o "$TMP/$CGM_ENGINE_ASSET"
echo "${CGM_ENGINE_ARCHIVE_SHA256}  $TMP/$CGM_ENGINE_ASSET" | sha256sum --check -
tar -xzf "$TMP/$CGM_ENGINE_ASSET" -C "$TMP"
ENGINE="$(find "$TMP" -type f -name "$CGM_ENGINE_BINARY" | head -n 1)"
[[ -n "$ENGINE" ]] || { echo "Configured Stockfish binary was not found after extraction." >&2; exit 1; }
chmod +x "$ENGINE"
mkdir -p "$(dirname "$INSTALL_PATH")"
install -m 0755 "$ENGINE" "$INSTALL_PATH"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m chessgrandmaster.engine_manifest verify "$INSTALL_PATH"
