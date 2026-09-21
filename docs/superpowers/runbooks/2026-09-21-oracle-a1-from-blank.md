# Oracle A1 Worker From Blank

Date: 2026-09-21
Target host: `Oracle-Chess`
Purpose: reproducible setup for a ChessGrandmaster B1 analysis worker.

This document records the setup actually validated on the Oracle instance.
It is intentionally rootless after the initial OS/hostname steps, so the
same approach remains usable on notebook/VPS environments with limited sudo.

## Final validated machine

- Oracle shape: `VM.Standard.A1.Flex`
- Region: `ap-singapore-2`
- CPU: 2 OCPU, ARM64 Neoverse-N1
- RAM: 12 GB
- Network allocation: 2 Gbps
- OS: Ubuntu 24.04.4 LTS
- Kernel: `6.17.0-1020-oracle`
- Root filesystem: ext4, 184 GB usable
- Free disk after setup: about 179 GB
- Swap: none
- Python: 3.12.3
- Node: 22.23.2 through nvm
- Desktop Commander: 0.2.51
- Stockfish: official Stockfish 19 ARM64 universal
## Runtime decision

This machine has only 2 OCPU even though RAM is generous.

Use one B1 job at a time with:

```text
workers = 2
threads = 1
Hash = 1536 MB per worker
depth = 19
```

Do not run two B1 analysis jobs concurrently on this VM.
The two 1-thread Stockfish workers are intended to occupy the two OCPUs
without CPU oversubscription. About 3 GB total Hash leaves ample RAM for
Python, SQLite, filesystem cache and output archives.

These values are runtime settings only. They must not be embedded into
`cgm-job-1` or the analysis config hash.

No new Stockfish performance benchmark was run during this setup.
## 1. Start from the Oracle Ubuntu user

Expected login user:

```bash
whoami
# ubuntu
```

Inspect the machine:

```bash
hostnamectl
lscpu
free -h
df -hT /
python3 --version
git --version
```

Oracle instance metadata can confirm the assigned resources:

```bash
python3 - <<'PY'
import json, urllib.request
req = urllib.request.Request(
    "http://169.254.169.254/opc/v2/instance/",
    headers={"Authorization": "Bearer Oracle"},
)
d = json.load(urllib.request.urlopen(req, timeout=5))
print(d["shape"])
print(d["shapeConfig"])
print(d["region"])
PY
```
## 2. Install Node with nvm

The validated machine uses nvm 0.40.3 and Node 22.23.2.
Install the same nvm release, then load it:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash

export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

nvm install 22
nvm alias default 22
node --version
npm --version
```

Verify `~/.bashrc` contains the normal nvm loader:

```bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
[ -s "$NVM_DIR/bash_completion" ] && . "$NVM_DIR/bash_completion"
```

Desktop Commander requires Node 18 or newer.
## 3. Pair Remote Desktop Commander once

Run interactively over SSH:

```bash
npx @wonderwhy-er/desktop-commander@0.2.51 remote
```

Open the printed verification URL in a browser logged into the same
Desktop Commander account and approve the device.

The persistent device state is stored in:

```text
~/.desktop-commander-device/device.json
```

It contains authentication tokens and must remain mode 0600.

Verify:

```bash
stat -c '%a %U:%G %n' ~/.desktop-commander-device/device.json
```

Expected mode: `600 ubuntu:ubuntu`.
## 4. Give the host a useful device name

Desktop Commander derives the displayed device name from the Linux hostname.

Over SSH:

```bash
sudo hostnamectl set-hostname Oracle-Chess
```

A later reconnect will appear as `Oracle-Chess` while preserving the
same Desktop Commander device ID.

The validated device ID is intentionally not needed by any project script.
Do not hardcode it into application configuration.
## 5. Pin Desktop Commander outside the npx cache

The first interactive run may execute from `~/.npm/_npx`.
Do not depend on that cache for reboot recovery.

Install a fixed copy in user space:

```bash
mkdir -p ~/.local/desktop-commander ~/.local/bin \
         ~/.local/state/desktop-commander

npm install --prefix ~/.local/desktop-commander \
  @wonderwhy-er/desktop-commander@0.2.51
```

The stable CLI used by the watchdog is:

```text
~/.local/desktop-commander/node_modules/@wonderwhy-er/desktop-commander/dist/index.js
```
## 6. Install the reboot/restart watchdog

Create `~/.local/bin/desktop-commander-remote-watchdog`:

```bash
#!/usr/bin/env bash
set -u
umask 077
NODE="$HOME/.nvm/versions/node/v22.23.2/bin/node"
CLI="$HOME/.local/desktop-commander/node_modules/@wonderwhy-er/desktop-commander/dist/index.js"
STATE="$HOME/.local/state/desktop-commander"
LOCK="$HOME/.desktop-commander-device/remote.lock"

mkdir -p "$STATE"
exec 9>"$LOCK"
/usr/bin/flock -n 9 || exit 0

while true; do
  printf '%s starting Desktop Commander remote\n' "$(date -Is)" >> "$STATE/watchdog.log"
  "$NODE" "$CLI" remote >> "$STATE/remote.log" 2>&1
  rc=$?
  printf '%s remote exited rc=%s; retrying in 10s\n' "$(date -Is)" "$rc" >> "$STATE/watchdog.log"
  sleep 10
done
```
Make it private/executable:

```bash
chmod 700 ~/.local/bin/desktop-commander-remote-watchdog
bash -n ~/.local/bin/desktop-commander-remote-watchdog
```

Use user cron for reboot startup:

```bash
printf '@reboot sleep 20 && %s/.local/bin/desktop-commander-remote-watchdog >/dev/null 2>&1\n' "$HOME" | crontab -
crontab -l
```

Why cron instead of a user systemd service:
on this Ubuntu image `systemctl --user` had no user bus/linger session.
Cron is already enabled and does not need an additional privileged setup.

The watchdog uses `flock`, so a second copy exits instead of registering
two remote processes.
## 7. Reboot acceptance test

Reboot once from SSH:

```bash
sudo reboot
```

The validated result on 2026-09-21 was:

- host returned as `Oracle-Chess`
- same Desktop Commander device ID
- persisted session restored without browser authorization
- watchdog started automatically about 20 seconds after boot
- Remote MCP marked the device online
- Desktop Commander local MCP child connected successfully

Useful diagnostics:

```bash
ps -ef | grep -E 'desktop-commander|remote-watchdog' | grep -v grep
tail -80 ~/.local/state/desktop-commander/watchdog.log
tail -120 ~/.local/state/desktop-commander/remote.log
```
Expected remote log contains:

```text
Found persisted session
Session restored
Device marked as online
Device ready
Device Name: Oracle-Chess
```

This reboot test is the acceptance gate for unattended reconnect.

## 8. Clone the project

```bash
mkdir -p ~/projects ~/work ~/data ~/logs

git clone --branch chatgpt-work --single-branch \
  https://github.com/chessfancy/gm-analyzer-b1.git \
  ~/projects/gm-analyzer-b1

cd ~/projects/gm-analyzer-b1
git status --short --branch
git rev-parse HEAD
```

At setup time the validated branch HEAD was
`f57b79a9ff7bd260f0543fb9ece199f53dfe742b`.
Always fetch and compare with `origin/chatgpt-work` before production work.
## 9. Python environment on a blank Ubuntu image

This Ubuntu image had Python 3.12 but did not have `python3-venv` or pip.
A direct `python3 -m venv .venv` therefore failed.

Instead of requiring sudo, install uv in user space:

```bash
curl -LsSf https://astral.sh/uv/install.sh -o ~/work/install-uv.sh
sh ~/work/install-uv.sh
~/.local/bin/uv --version
```

Create the environment and install the project:

```bash
cd ~/projects/gm-analyzer-b1
rm -rf .venv
~/.local/bin/uv venv .venv --python /usr/bin/python3.12
~/.local/bin/uv pip install --python .venv/bin/python -e '.[dev]'
```

Validated test result on Oracle:

```text
334 passed, 1 skipped
```
Run the checks yourself with:

```bash
cd ~/projects/gm-analyzer-b1
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests
```

Note: the repository `scripts/setup_platform.sh` still assumes that
either stdlib venv/ensurepip works or a bootstrap pip is already available.
The uv path above is the proven blank-Ubuntu workaround.

## 10. Install the pinned ARM64 Stockfish

The repository already contains the authoritative Stockfish 19 asset,
archive checksum and ARM64 platform mapping in
`src/chessgrandmaster/engine.toml`.

Install it using the project script:

```bash
cd ~/projects/gm-analyzer-b1
CGM_PYTHON=.venv/bin/python bash scripts/install_stockfish.sh
```
Validated installed path:

```text
~/.local/share/chessgrandmaster/bin/stockfish
```

Verify without running a benchmark:

```bash
.venv/bin/cgm-engine info
.venv/bin/cgm-engine verify \
  ~/.local/share/chessgrandmaster/bin/stockfish
```

Validated engine identity:

```text
Stockfish 19
SHA256 bb6599ef38b7a4ae79200a601c40e81a854219a49dba78b1110e7eb4620609bf
Status OK
```

Do not benchmark Stockfish merely to choose resource settings.
## 11. Create the worker filesystem layout

```bash
mkdir -p ~/data/cgm/{inbox,jobs,work,results,archive,logs}
mkdir -p ~/.config/chessgrandmaster
```

Create `~/.config/chessgrandmaster/oracle-a1.env`:

```bash
export CGM_HOME="$HOME/data/cgm"
export CGM_STOCKFISH="$HOME/.local/share/chessgrandmaster/bin/stockfish"
export CGM_RUNTIME_WORKERS="2"
export CGM_RUNTIME_THREADS="1"
export CGM_RUNTIME_HASH_MB="1536"
export CGM_RUNTIME_DEPTH="19"
export CGM_REPO="$HOME/projects/gm-analyzer-b1"
```

Protect it:

```bash
chmod 600 ~/.config/chessgrandmaster/oracle-a1.env
```
These variables document runtime policy. The current B1 CLI accepts
`--workers`, `--threads`, `--hash-mb` and `--depth` explicitly;
future B2b/worker wrappers should translate the runtime environment into
those CLI options rather than changing JobSpec.

## 12. Health helper

This host has:

```text
~/.local/bin/cgm-oracle-status
```

Run:

```bash
~/.local/bin/cgm-oracle-status
```

It reports:

- hostname and uptime
- RAM and root disk
- repository branch/HEAD
- Stockfish verification
- Desktop Commander watchdog/process state
- runtime data-directory size
## 13. Security/operational notes

- Desktop Commander device credentials stay only in the ubuntu home directory.
- Desktop Commander state/log directories are mode 0700 and logs are mode 0600.
- Do not commit `~/.desktop-commander-device/device.json`.
- Do not put cloud/object-store credentials in JobSpec, manifests or Git.
- Keep the Oracle VCN/security rules restrictive; analysis needs outbound HTTPS
  and SSH administration, not a public application port.
- This image has `PasswordAuthentication no` and `KbdInteractiveAuthentication no`; keep SSH key-only.
- SSH is socket-activated (`ssh.socket`) and was observed receiving routine Internet
  scan attempts. If practical, restrict OCI TCP/22 ingress to your administrative IP/CIDR.
- `unattended-upgrades` is enabled and active on the validated image.
- The host currently has no swap. With 12 GB RAM and the selected 2 x 1536 MB
  Hash profile, swap is not required for the initial worker.
- Do not auto-pull Git while a production job is running.
- Do not run acquisition databases from the Windows production corpus directly
  on this worker; transfer packaged immutable jobs instead.
- Do not run multiple CPU-heavy chats/jobs on this 2-OCPU VM while B1 is active.

## 14. Production worker workflow after B2b exists

Target flow:

```text
cgm-job-1 + shard input.pgn
-> one Oracle job at a time
-> B1: 2 workers x 1 thread, Hash 1536 MB each, depth 19
-> analysis.sqlite
-> raw UCI archive
-> Mistakes.pgn
-> Blunders.pgn
-> checksummed result bundle
-> upload/return by job id + config hash
```
The first production acceptance must be ONE packaged shard only.
Verify input checksum, output checksums and canonical-game mapping before
opening the queue to more shards.

After that acceptance, Oracle becomes the first always-on worker while
Deepnote, Kaggle and Molab/Marimo consume the same provider-neutral jobs.

## 15. Troubleshooting

If Remote Desktop Commander is offline:

```bash
crontab -l
ps -ef | grep -E 'desktop-commander|remote-watchdog' | grep -v grep
tail -100 ~/.local/state/desktop-commander/watchdog.log
tail -200 ~/.local/state/desktop-commander/remote.log
```

If necessary, start the watchdog manually:

```bash
~/.local/bin/desktop-commander-remote-watchdog >/dev/null 2>&1 &
```

Because it uses `flock`, this is safe when another watchdog is already active.
If the persisted session has genuinely been revoked, run the interactive
pairing command again:

```bash
npx @wonderwhy-er/desktop-commander@0.2.51 remote
```

and approve the new browser verification. Do not delete the device JSON
during normal maintenance.

If the project environment is damaged:

```bash
cd ~/projects/gm-analyzer-b1
rm -rf .venv
~/.local/bin/uv venv .venv --python /usr/bin/python3.12
~/.local/bin/uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q
```

Then verify Stockfish separately with `cgm-engine verify`.

## Current setup gate

As of 2026-09-21 the VM has passed:
reboot reconnect, repository checkout, full Python tests, compileall,
official Stockfish 19 ARM64 verification, outbound GitHub HTTPS and
runtime directory setup.

The next dependency is B2b packaging; no large Stockfish analysis should
start before a real `cgm-job-1` shard is accepted end-to-end.
