#!/usr/bin/env bash
set -euo pipefail

VERSION="19"
TAG="sf_19"

ARCH="$(uname -m)"

case "$ARCH" in
    x86_64|amd64)
        ASSET="stockfish-linux-x86-64-universal.tar.gz"
        BINARY="stockfish-linux-x86-64-universal"
        SHA256="9defc0d4e55d49c65a6d042f3e571a39fcea499ade6dbe741b53b8c65e03611f"
        ;;
    aarch64|arm64)
        ASSET="stockfish-linux-arm64-universal.tar.gz"
        BINARY="stockfish-linux-arm64-universal"
        SHA256="fe26cfd1d9db4c8af3d21e24d9ff34cacb31c1f940085a7583da11796f2bac01"
        ;;
    *)
        echo "Unsupported architecture: $ARCH" >&2
        exit 1
        ;;
esac

URL="https://github.com/official-stockfish/Stockfish/releases/download/${TAG}/${ASSET}"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Installing Stockfish ${VERSION}"
echo "Architecture: ${ARCH}"

curl -fL \
    --retry 3 \
    --retry-delay 2 \
    "$URL" \
    -o "$TMP/$ASSET"

echo "${SHA256}  $TMP/$ASSET" | sha256sum --check -

tar -xzf "$TMP/$ASSET" -C "$TMP"

ENGINE="$(find "$TMP" -type f -name "$BINARY" | head -n 1)"

if [[ -z "$ENGINE" ]]; then
    echo "Stockfish binary not found after extraction." >&2
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
stockfish <<'UCI' | grep -m1 '^id name'
uci
quit
UCI
