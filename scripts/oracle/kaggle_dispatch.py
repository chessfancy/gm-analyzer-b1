#!/usr/bin/env python3
"""Lease one coordinator job, run it on Kaggle, and import the verified result."""

from __future__ import annotations

import argparse
import hashlib
import json
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
EXPORTS = CGM / "exports/kaggle"
IMPORTS = CGM / "imports/kaggle"
TRANSPORT = CGM / "transports/kaggle"
REPO = HOME / "projects/gm-analyzer-b1"
KAGGLE = HOME / ".local/bin/kaggle"


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


def _username() -> str:
    proc = _run([KAGGLE, "config", "view"], timeout=30)
    match = re.search(r"^- username:\s*(\S+)\s*$", proc.stdout, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("Kaggle username not found in CLI config")
    return match.group(1)


def _status(kernel_ref: str) -> str:
    proc = _run([KAGGLE, "kernels", "status", kernel_ref], timeout=30)
    match = re.search(r"KernelWorkerStatus\.([A-Z_]+)", proc.stdout)
    if not match:
        raise RuntimeError(f"could not parse Kaggle status: {proc.stdout!r}")
    return match.group(1)


def _kernel_script(repo_sha: str) -> str:
    return f'''from pathlib import Path
import os, shutil, subprocess

INPUT_ROOT = Path("/kaggle/input")
WORKING = Path("/kaggle/working")
REPO = WORKING / "gm-analyzer-b1"
VENV = WORKING / ".venv"
RESULT = WORKING / "result-bundle"
SHA = {repo_sha!r}

def run(cmd, cwd=None, env=None):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)

jobs = sorted(INPUT_ROOT.rglob("job.json"))
if len(jobs) != 1:
    raise RuntimeError(f"expected one job.json under {{INPUT_ROOT}}, got {{jobs}}")
bundle = jobs[0].parent
print("JOB_BUNDLE", bundle, flush=True)

for path in (REPO, VENV, RESULT):
    if path.exists():
        shutil.rmtree(path)
run(["git", "clone", "https://github.com/chessfancy/gm-analyzer-b1.git", str(REPO)])
run(["git", "checkout", SHA], cwd=REPO)

env = os.environ.copy()
env["CGM_VENV"] = str(VENV)
run(["bash", "scripts/setup_platform.sh"], cwd=REPO, env=env)
run([
    str(VENV / "bin" / "python"),
    str(REPO / "scripts/workers/run_job_bundle.py"),
    "--bundle", str(bundle),
    "--result", str(RESULT),
    "--provider", "kaggle",
], cwd=REPO, env={{**env, "PYTHONPATH": str(REPO / "src")}})

# Only the portable result bundle should remain in /kaggle/working.
shutil.rmtree(REPO)
shutil.rmtree(VENV)
print("RESULT_READY", RESULT, flush=True)
'''


def _fail_for_retry(coordinator: Coordinator, job_id: str, reason: str) -> None:
    try:
        coordinator.fail_attempt(job_id, reason[:4000], retry=True)
    except Exception:
        # The job may already be terminal (for example after a successful import).
        pass


def _cleanup_remote(dataset_ref: str, kernel_ref: str) -> None:
    _run([KAGGLE, "kernels", "delete", "-y", kernel_ref], timeout=60, check=False)
    _run([KAGGLE, "datasets", "delete", "-y", dataset_ref], timeout=60, check=False)


def dispatch_one(
    *,
    poll_seconds: int = 30,
    timeout_seconds: int = 14400,
    keep_remote: bool = False,
) -> int:
    username = _username()
    repo_sha = _run(["git", "-C", REPO, "rev-parse", "HEAD"], timeout=30).stdout.strip()
    coordinator = Coordinator(COORD_DB, archive_root=ARCHIVE)
    lease = coordinator.lease_next(
        "kaggle-api",
        "kaggle",
        lease_seconds=timeout_seconds + 1800,
    )
    if lease is None:
        print(json.dumps({"ok": True, "message": "no pending job"}))
        return 0

    token = hashlib.sha256(
        f"{lease.job_id}:{lease.attempt_number}".encode()
    ).hexdigest()[:12]
    dataset_slug = f"cgm-job-{token}"
    kernel_slug = f"cgm-worker-{token}"
    dataset_ref = f"{username}/{dataset_slug}"
    kernel_ref = f"{username}/{kernel_slug}"
    export_dir = EXPORTS / token
    dataset_dir = TRANSPORT / f"dataset-{token}"
    kernel_dir = TRANSPORT / f"kernel-{token}"

    try:
        if export_dir.exists():
            shutil.rmtree(export_dir)
        coordinator.export_job(lease.job_id, export_dir)

        for path in (dataset_dir, kernel_dir):
            if path.exists():
                shutil.rmtree(path)
            path.mkdir(parents=True)

        for source in sorted(export_dir.iterdir()):
            if source.is_file():
                shutil.copy2(source, dataset_dir / source.name)
        (dataset_dir / "dataset-metadata.json").write_text(
            json.dumps(
                {
                    "title": dataset_slug,
                    "id": dataset_ref,
                    "licenses": [{"name": "CC0-1.0"}],
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        _run(
            [KAGGLE, "datasets", "create", "-p", dataset_dir, "-q", "-r", "zip"],
            timeout=180,
        )

        ready_deadline = time.monotonic() + 180
        while True:
            probe = _run(
                [KAGGLE, "datasets", "files", dataset_ref],
                timeout=30,
                check=False,
            )
            if probe.returncode == 0 and "job.json" in probe.stdout:
                break
            if time.monotonic() >= ready_deadline:
                raise TimeoutError("Kaggle dataset did not become ready")
            time.sleep(5)

        (kernel_dir / "worker.py").write_text(
            _kernel_script(repo_sha),
            encoding="utf-8",
        )
        (kernel_dir / "kernel-metadata.json").write_text(
            json.dumps(
                {
                    "id": kernel_ref,
                    "title": kernel_slug,
                    "code_file": "worker.py",
                    "language": "python",
                    "kernel_type": "script",
                    "is_private": True,
                    "enable_gpu": False,
                    "enable_internet": True,
                    "dataset_sources": [dataset_ref],
                    "competition_sources": [],
                    "kernel_sources": [],
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

        _run([KAGGLE, "kernels", "push", "-p", kernel_dir], timeout=180)
        coordinator.mark_running(lease.job_id)

        deadline = time.monotonic() + timeout_seconds
        status = "RUNNING"
        while status not in {"COMPLETE", "ERROR", "CANCELLED"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Kaggle kernel {kernel_ref} timed out")
            time.sleep(poll_seconds)
            status = _status(kernel_ref)
            print("STATUS", status, flush=True)

        if status != "COMPLETE":
            logs = _run(
                [KAGGLE, "kernels", "logs", kernel_ref],
                timeout=60,
                check=False,
            )
            raise RuntimeError(
                f"Kaggle kernel ended {status}: "
                f"{(logs.stdout + logs.stderr)[-2000:]}"
            )

        output_dir = IMPORTS / token
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)
        _run(
            [
                KAGGLE,
                "kernels",
                "output",
                kernel_ref,
                "-p",
                output_dir,
                "--force",
                "--file-pattern",
                "^result-bundle/.*",
            ],
            timeout=300,
        )
        candidates = list(output_dir.rglob("job-result.json"))
        if len(candidates) != 1:
            raise RuntimeError(
                f"expected one Kaggle job-result.json, got {candidates}"
            )
        receipt = coordinator.import_result(candidates[0].parent)
    except Exception as exc:
        _fail_for_retry(coordinator, lease.job_id, f"Kaggle dispatch: {exc}")
        raise
    finally:
        shutil.rmtree(dataset_dir, ignore_errors=True)
        shutil.rmtree(kernel_dir, ignore_errors=True)

    if not keep_remote:
        _cleanup_remote(dataset_ref, kernel_ref)

    print(
        json.dumps(
            {
                "ok": True,
                "provider": "kaggle",
                "job_id": receipt.job_id,
                "attempt": receipt.attempt_number,
                "state": receipt.state.value,
                "dataset": dataset_ref,
                "kernel": kernel_ref,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=int, default=14400)
    parser.add_argument(
        "--keep-remote",
        action="store_true",
        help="keep Kaggle dataset/kernel after a successful import",
    )
    args = parser.parse_args(argv)
    return dispatch_one(
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        keep_remote=args.keep_remote,
    )


if __name__ == "__main__":
    raise SystemExit(main())
