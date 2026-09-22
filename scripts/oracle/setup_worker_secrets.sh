#!/usr/bin/env bash
set -euo pipefail
umask 077

KAGGLE_DIR="$HOME/.kaggle"
CGM_SECRET_DIR="$HOME/.config/cgm/secrets"
KAGGLE_FILE="$KAGGLE_DIR/access_token"
DEEPNOTE_FILE="$CGM_SECRET_DIR/deepnote_api_key"

mkdir -p "$KAGGLE_DIR" "$CGM_SECRET_DIR"
chmod 700 "$KAGGLE_DIR" "$HOME/.config/cgm" "$CGM_SECRET_DIR"

echo "ChessGrandmaster external-worker credentials"
echo "Secrets are read silently and written only to local 0600 files."
echo

read -r -s -p "Paste Kaggle API access token: " kaggle_token
echo
if [[ -z "$kaggle_token" ]]; then
  echo "Kaggle token was empty; aborting." >&2
  exit 1
fi
printf '%s\n' "$kaggle_token" > "$KAGGLE_FILE"
chmod 600 "$KAGGLE_FILE"
unset kaggle_token

read -r -s -p "Paste Deepnote API key: " deepnote_token
echo
if [[ -z "$deepnote_token" ]]; then
  echo "Deepnote token was empty; removing partial Deepnote credential." >&2
  rm -f "$DEEPNOTE_FILE"
  exit 1
fi
printf '%s\n' "$deepnote_token" > "$DEEPNOTE_FILE"
chmod 600 "$DEEPNOTE_FILE"
unset deepnote_token

echo
echo "Saved:"
echo "  $KAGGLE_FILE"
echo "  $DEEPNOTE_FILE"
echo "No secret values were printed."
