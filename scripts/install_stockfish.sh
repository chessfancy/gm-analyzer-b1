#!/usr/bin/env bash
set -euo pipefail

ROOT="$(
    cd "$(dirname "${BASH_SOURCE[0]}")/.."
    pwd
)"

cd "$ROOT"

eval "$(
    PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
        python -m chessgrandmaster.engine_manifest \
        shell-install-spec
)"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Installing ${CGM_ENGINE_LABEL}"
echo "Asset       : ${CGM_ENGINE_ASSET}"
echo "Download    : ${CGM_ENGINE_URL}"

curl -fL \
    --retry 3 \
    --retry-delay 2 \
    "$CGM_ENGINE_URL" \
    -o "$TMP/$CGM_ENGINE_ASSET"

echo \
    "${CGM_ENGINE_ARCHIVE_SHA256}  $TMP/$CGM_ENGINE_ASSET" \
    | sha256sum --check -

tar -xzf \
    "$TMP/$CGM_ENGINE_ASSET" \
    -C "$TMP"

ENGINE="$(
    find "$TMP" \
        -type f \
        -name "$CGM_ENGINE_BINARY" \
        | head -n 1
)"

if [[ -z "$ENGINE" ]]; then
    echo "Configured Stockfish binary not found after extraction." >&2
    exit 1
fi

chmod +x "$ENGINE"

sudo install \
    -m 0755 \
    "$ENGINE" \
    /usr/local/bin/stockfish

echo
echo "Installed:"
command -v stockfish

echo
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    python -m chessgrandmaster.engine_manifest \
    verify \
    /usr/local/bin/stockfish
