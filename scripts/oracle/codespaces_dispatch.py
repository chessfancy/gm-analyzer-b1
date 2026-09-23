#!/usr/bin/env python3
"""Lease one coordinator job, run it in a persistent GitHub Codespace, and import the result."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from chessgrandmaster.coordinator import Coordinator


HOME = Path.home()
CGM = HOME / "data/cgm"
COORD_DB = CGM / "coordinator.sqlite"
ARCHIVE = CGM / "archive"
EXPORTS = CGM / "exports/codespaces"
IMPORTS = CGM / "imports/codespaces"
LOCK = CGM / "locks/codespaces-dispatch.lock"
CONFIG = HOME / ".config/cgm/codespaces-worker.json"
REPO = HOME / "projects/gm-analyzer-b1"
GH = Path(os.environ.get("CGM_GH", str(HOME / ".local/bin/gh")))


def _run(args, *, timeout: int = 300, check: bool = True):
    proc = subprocess.run(
        [str(value) for value in args],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed rc={proc.returncode}: {' '.join(map(str, args))}\n"
            f"stdout={proc.stdout[-4000:]}\nstderr={proc.stderr[-4000:]}"
        )
    return proc


def _codespace_state(name: str) -> str:
    proc = _run(
        [GH, "api", f"/user/codespaces/{name}"],
        timeout=30,
    )
    return str(json.loads(proc.stdout)["state"])


def _ensure_available(
    name: str,
    *,
    poll_seconds: int = 5,
    timeout_seconds: int = 600,
) -> None:
    state = _codespace_state(name)
    if state == "Available":
        return
    if state in {"Shutdown", "Stopped"}:
        _run(
            [GH, "api", "--method", "POST", f"/user/codespaces/{name}/start"],
            timeout=60,
        )

    deadline = time.monotonic() + timeout_seconds
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Codespace {name} did not become Available; last state={state}"
            )
        time.sleep(poll_seconds)
        state = _codespace_state(name)
        print("CODESPACE", name, state, flush=True)
        if state == "Available":
            return
        if state in {"Failed", "Deleted"}:
            raise RuntimeError(f"Codespace {name} entered terminal state {state}")


def _ssh(name: str, command: str, *, timeout: int = 300):
    return _run(
        [GH, "codespace", "ssh", "-c", name, "--", command],
        timeout=timeout,
    )


def _copy_to_remote(name: str, export_dir: Path, remote_dir: str) -> None:
    files = sorted(path for path in export_dir.iterdir() if path.is_file())
    if not files:
        raise RuntimeError(f"no job bundle files found in {export_dir}")
    _ssh(name, f"rm -rf {remote_dir} && mkdir -p {remote_dir}", timeout=60)
    _run(
        [
            GH,
            "codespace",
            "cp",
            "-e",
            "-c",
            name,
            *files,
            f"remote:{remote_dir}/",
        ],
        timeout=300,
    )


def _copy_from_remote(
    name: str,
    remote_dir: str,
    local_parent: Path,
) -> Path:
    local_parent.mkdir(parents=True, exist_ok=True)
    local_root = local_parent / Path(remote_dir).name
    if local_root.exists():
        shutil.rmtree(local_root)
    _run(
        [
            GH,
            "codespace",
            "cp",
            "-e",
            "-r",
            "-c",
            name,
            f"remote:{remote_dir}",
            local_parent,
        ],
        timeout=600,
    )
    if not local_root.exists():
        raise RuntimeError(
            f"Codespaces result copy did not create expected path {local_root}"
        )
    return local_root


def _remote_command(token: str, repo_sha: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", token):
        raise ValueError(f"invalid attempt token {token!r}")
    if not re.fullmatch(r"[0-9a-f]{40}", repo_sha):
        raise ValueError(f"invalid repository SHA {repo_sha!r}")
    return f"""set -euo pipefail
REPO=/workspaces/gm-analyzer-b1
JOB="$HOME/cgm-worker/jobs/{token}"
RESULT="$HOME/cgm-worker/results/{token}"
cd "$REPO"
git fetch --quiet origin chatgpt-work
git reset --hard {repo_sha}
if [[ ! -x .venv/bin/python ]]; then
    bash scripts/setup_platform.sh
fi
ENGINE="$HOME/.local/share/chessgrandmaster/bin/stockfish"
PYTHONPATH=src .venv/bin/python -m chessgrandmaster.engine_manifest verify "$ENGINE"
rm -rf "$RESULT"
PYTHONPATH=src .venv/bin/python scripts/workers/run_job_bundle.py \
    --bundle "$JOB" \
    --result "$RESULT" \
    --provider codespaces
"""


def _fail_for_retry(coordinator: Coordinator, job_id: str, reason: str) -> None:
    try:
        coordinator.fail_attempt(job_id, reason[:4000], retry=True)
    except Exception:
        pass


def _stop_codespace(name: str) -> None:
    _run([GH, "codespace", "stop", "-c", name], timeout=60, check=False)


def dispatch_one(
    codespace_name: str,
    *,
    poll_seconds: int = 5,
    timeout_seconds: int = 14400,
    keep_remote: bool = False,
    keep_running: bool = False,
) -> int:
    if not GH.exists():
        raise FileNotFoundError(f"GitHub CLI not found: {GH}")

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)

        repo_sha = _run(
            ["git", "-C", REPO, "rev-parse", "HEAD"],
            timeout=30,
        ).stdout.strip()

        coordinator = Coordinator(COORD_DB, archive_root=ARCHIVE)
        lease = coordinator.lease_next(
            f"codespaces:{codespace_name}",
            "codespaces",
            lease_seconds=timeout_seconds + 1800,
        )
        if lease is None:
            print(json.dumps({"ok": True, "message": "no pending job"}))
            return 0

        token = hashlib.sha256(
            f"{lease.job_id}:{lease.attempt_number}".encode()
        ).hexdigest()[:12]
        export_dir = EXPORTS / token
        remote_job = f"~/cgm-worker/jobs/{token}"
        remote_result = f"~/cgm-worker/results/{token}"
        receipt = None

        try:
            _ensure_available(
                codespace_name,
                poll_seconds=poll_seconds,
                timeout_seconds=min(timeout_seconds, 900),
            )

            if export_dir.exists():
                shutil.rmtree(export_dir)
            coordinator.export_job(lease.job_id, export_dir)
            _copy_to_remote(codespace_name, export_dir, remote_job)

            coordinator.mark_running(lease.job_id)
            print("RUN", codespace_name, lease.job_id, flush=True)
            _ssh(
                codespace_name,
                _remote_command(token, repo_sha),
                timeout=timeout_seconds,
            )

            result_root = _copy_from_remote(
                codespace_name,
                remote_result,
                IMPORTS,
            )
            candidates = list(result_root.rglob("job-result.json"))
            if len(candidates) != 1:
                raise RuntimeError(
                    f"expected one Codespaces job-result.json, got {candidates}"
                )
            receipt = coordinator.import_result(candidates[0].parent)
        except Exception as exc:
            _fail_for_retry(
                coordinator,
                lease.job_id,
                f"Codespaces dispatch: {exc}",
            )
            raise
        else:
            if not keep_remote:
                _ssh(
                    codespace_name,
                    f"rm -rf {remote_job} {remote_result}",
                    timeout=60,
                )
        finally:
            if not keep_running:
                _stop_codespace(codespace_name)

        print(
            json.dumps(
                {
                    "ok": True,
                    "provider": "codespaces",
                    "job_id": receipt.job_id,
                    "attempt": receipt.attempt_number,
                    "state": receipt.state.value,
                    "codespace": codespace_name,
                },
                sort_keys=True,
            )
        )
        return 0


def _configured_codespace(value: str | None) -> str:
    if value:
        return value
    if CONFIG.exists():
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        name = str(cfg.get("codespace_name", "")).strip()
        if name:
            return name
    raise RuntimeError(
        "Codespace name is required via --codespace or "
        f"{CONFIG}"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codespace")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=14400)
    parser.add_argument(
        "--keep-remote",
        action="store_true",
        help="keep remote job/result directories after success",
    )
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="leave the Codespace running after the attempt",
    )
    args = parser.parse_args(argv)
    return dispatch_one(
        _configured_codespace(args.codespace),
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        keep_remote=args.keep_remote,
        keep_running=args.keep_running,
    )


if __name__ == "__main__":
    raise SystemExit(main())
