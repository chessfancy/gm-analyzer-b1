#!/usr/bin/env bash
set -euo pipefail
umask 077
DIR="/home/ubuntu/.config/cgm/secrets"
FILE="$DIR/molab_s3.env"
mkdir -p "$DIR"
printf 'Molab S3 Access Key ID: '
IFS= read -r ACCESS_KEY
printf 'Molab S3 Secret Key: '
IFS= read -rs SECRET_KEY
printf '\n'
[[ -n "$ACCESS_KEY" && -n "$SECRET_KEY" ]] || { echo 'Access/secret key must not be empty.' >&2; exit 1; }
{
  printf 'CGM_MOLAB_S3_ENDPOINT=%q\n' 'https://node02.s3interdata.com:9000'
  printf 'CGM_MOLAB_S3_BUCKET=%q\n' 's3-637-35690-storage'
  printf 'CGM_MOLAB_S3_PREFIX=%q\n' 'gm-analyzer/molab'
  printf 'CGM_MOLAB_S3_ACCESS_KEY=%q\n' "$ACCESS_KEY"
  printf 'CGM_MOLAB_S3_SECRET_KEY=%q\n' "$SECRET_KEY"
  printf 'CGM_MOLAB_MAX_JOBS=%q\n' '45'
} > "$FILE"
chmod 600 "$FILE"
echo "Saved Molab S3 credential config to $FILE (mode 0600)."
echo 'No secret was printed or passed on the command line.'
if [[ -x /home/ubuntu/projects/gm-analyzer-b1/.venv/bin/python ]]; then
  echo 'Credential file ready. Run the probe after boto3 is installed:'
  echo '  cd /home/ubuntu/projects/gm-analyzer-b1 && source /home/ubuntu/.config/cgm/secrets/molab_s3.env && PYTHONPATH=src .venv/bin/python scripts/oracle/probe_molab_s3.py'
fi
