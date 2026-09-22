#!/usr/bin/env python3
"""Dispatch one coordinator job to Deepnote and import its verified result."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from chessgrandmaster.coordinator import Coordinator

BASE = "https://api.deepnote.com/v2"
HOME = Path.home()
CGM = HOME / "data/cgm"
COORD_DB = CGM / "coordinator.sqlite"
ARCHIVE = CGM / "archive"
EXPORTS = CGM / "exports/deepnote"
IMPORTS = CGM / "imports/deepnote"
SECRET = HOME / ".config/cgm/secrets/deepnote_api_key"
CONFIG = HOME / ".config/cgm/deepnote-worker.json"
REPO = HOME / "projects/gm-analyzer-b1"


def _token() -> str:
    return SECRET.read_text(encoding="utf-8").strip()


def _headers(*, json_body: bool = False) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
        "User-Agent": "ChessGrandmaster coordinator/1.0",
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _request_json(method: str, path: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers=_headers(json_body=payload is not None),
        method=method,
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        if response.status == 204:
            return None
        return json.load(response)


def _upload_file(project_id: str, remote_path: str, local_path: Path) -> str:
    boundary = "----cgm-" + uuid.uuid4().hex
    buf = io.BytesIO()

    def field(name: str, value: str):
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        buf.write(value.encode())
        buf.write(b"\r\n")

    field("projectId", project_id)
    field("path", remote_path)
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{local_path.name}"\r\n'
        ).encode()
    )
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    buf.write(local_path.read_bytes())
    buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        BASE + "/files",
        data=buf.getvalue(),
        headers={
            **_headers(),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        created = json.load(response)
    return created["file"]["path"]


def _download_file(project_id: str, remote_path: str, local_path: Path) -> None:
    query = urllib.parse.urlencode({"projectId": project_id, "path": remote_path})
    req = urllib.request.Request(
        BASE + "/files/download?" + query,
        headers=_headers(),
    )
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=120) as response:
        local_path.write_bytes(response.read())


def _remote_code(
    job_dir: str,
    result_zip: str,
    repo_sha: str,
    attempt_tag: str,
) -> str:
    return f'''from pathlib import Path
import os, shutil, subprocess

WORK = Path("/work")
RUNTIME = WORK / "cgm-worker" / "runtime" / {attempt_tag!r}
REPO = RUNTIME / "repo"
VENV = RUNTIME / ".venv"
BUNDLE = WORK / {job_dir!r}
RESULT = RUNTIME / "result-bundle"
ZIP_BASE = WORK / {result_zip[:-4]!r}
SHA = {repo_sha!r}

def run(cmd, cwd=None, env=None):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, cwd=cwd, env=env, check=True)

if RUNTIME.exists():
    shutil.rmtree(RUNTIME)
RUNTIME.mkdir(parents=True, exist_ok=True)
run(["git", "clone", "https://github.com/chessfancy/gm-analyzer-b1.git", str(REPO)])
run(["git", "reset", "--hard", SHA], cwd=REPO)

env = os.environ.copy()
env["CGM_VENV"] = str(VENV)
run(["bash", "scripts/setup_platform.sh"], cwd=REPO, env=env)

if RESULT.exists():
    shutil.rmtree(RESULT)
python = VENV / "bin" / "python"
run([
    str(python),
    str(REPO / "scripts/workers/run_job_bundle.py"),
    "--bundle", str(BUNDLE),
    "--result", str(RESULT),
    "--provider", "deepnote",
], cwd=REPO, env={{**env, "PYTHONPATH": str(REPO / "src")}})

ZIP_BASE.parent.mkdir(parents=True, exist_ok=True)
zip_path = shutil.make_archive(str(ZIP_BASE), "zip", root_dir=RESULT.parent, base_dir=RESULT.name)
print("RESULT_ZIP", zip_path)
shutil.rmtree(RUNTIME)
'''


def dispatch_one(*, poll_seconds: int = 10, timeout_seconds: int = 14400) -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    project_id = cfg["project_id"]
    notebook_id = cfg["notebook_id"]
    block_id = cfg["block_id"]

    repo_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
    ).strip()

    coordinator = Coordinator(COORD_DB, archive_root=ARCHIVE)
    lease = coordinator.lease_next(
        "deepnote-api",
        "deepnote",
        lease_seconds=timeout_seconds + 1800,
    )
    if lease is None:
        print(json.dumps({"ok": True, "message": "no pending job"}))
        return 0

    attempt_tag = f"attempt-{lease.attempt_id}-{uuid.uuid4().hex[:8]}"
    export_dir = EXPORTS / attempt_tag
    if export_dir.exists():
        shutil.rmtree(export_dir)
    coordinator.export_job(lease.job_id, export_dir)

    remote_dir = f"cgm-worker/jobs/{attempt_tag}"
    for local in sorted(export_dir.iterdir()):
        if local.is_file():
            _upload_file(project_id, f"{remote_dir}/{local.name}", local)

    remote_zip = f"cgm-worker/results/{attempt_tag}.zip"
    content = _remote_code(remote_dir, remote_zip, repo_sha, attempt_tag)
    _request_json("PATCH", f"/blocks/{block_id}", {"content": content})

    created = _request_json(
        "POST",
        "/runs",
        {
            "notebookId": notebook_id,
            "detached": True,
            "detachedRunStorageMode": "read_write",
        },
    )
    run_id = created["runId"]
    coordinator.mark_running(lease.job_id)
    print("RUN", run_id, lease.job_id, flush=True)

    deadline = time.monotonic() + timeout_seconds
    status = created["status"]
    while status not in {"success", "error", "internal_error", "stopped"}:
        if time.monotonic() >= deadline:
            coordinator.fail_attempt(lease.job_id, "Deepnote run timeout")
            raise TimeoutError(f"Deepnote run {run_id} timed out")
        time.sleep(poll_seconds)
        run = _request_json("GET", f"/runs/{run_id}")["run"]
        status = run["status"]
        print("STATUS", status, flush=True)

    if status != "success":
        coordinator.fail_attempt(lease.job_id, f"Deepnote run ended {status}")
        raise RuntimeError(f"Deepnote run {run_id} ended {status}")

    local_zip = IMPORTS / f"{attempt_tag}.zip"
    _download_file(project_id, remote_zip, local_zip)
    result_root = IMPORTS / attempt_tag
    if result_root.exists():
        shutil.rmtree(result_root)
    result_root.mkdir(parents=True)
    with zipfile.ZipFile(local_zip) as zf:
        zf.extractall(result_root)

    candidates = list(result_root.rglob("job-result.json"))
    if len(candidates) != 1:
        coordinator.fail_attempt(lease.job_id, "invalid Deepnote result archive")
        raise RuntimeError(f"expected one job-result.json, got {candidates}")
    bundle = candidates[0].parent
    receipt = coordinator.import_result(bundle)
    print(
        json.dumps(
            {
                "ok": True,
                "provider": "deepnote",
                "job_id": receipt.job_id,
                "attempt": receipt.attempt_number,
                "state": receipt.state.value,
                "run_id": run_id,
            },
            sort_keys=True,
        )
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=int, default=14400)
    args = parser.parse_args(argv)
    return dispatch_one(
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
