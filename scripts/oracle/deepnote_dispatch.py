#!/usr/bin/env python3
"""Lease one coordinator job, run it on Deepnote, and import the verified result."""

from __future__ import annotations

import argparse
import fcntl
import io
import json
from pathlib import Path
import shutil
import subprocess
import time
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
LOCK = CGM / "locks/deepnote-submit.lock"
SECRET = HOME / ".config/cgm/secrets/deepnote_api_key"
CONFIG = HOME / ".config/cgm/deepnote-worker.json"
REPO = HOME / "projects/gm-analyzer-b1"


def _token() -> str:
    return SECRET.read_text(encoding="utf-8").strip()


def _headers(*, json_body: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
        "User-Agent": "ChessGrandmaster coordinator/1.0",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


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

    def field(name: str, value: str) -> None:
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
    with urllib.request.urlopen(req, timeout=180) as response:
        local_path.write_bytes(response.read())


def _delete_file(project_id: str, remote_path: str) -> None:
    query = urllib.parse.urlencode({"projectId": project_id, "path": remote_path})
    req = urllib.request.Request(
        BASE + "/files?" + query,
        headers=_headers(),
        method="DELETE",
    )
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except Exception:
        pass


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

python = VENV / "bin" / "python"
run([
    str(python),
    str(REPO / "scripts/workers/run_job_bundle.py"),
    "--bundle", str(BUNDLE),
    "--result", str(RESULT),
    "--provider", "deepnote",
], cwd=REPO, env={{**env, "PYTHONPATH": str(REPO / "src")}})

ZIP_BASE.parent.mkdir(parents=True, exist_ok=True)
zip_path = shutil.make_archive(
    str(ZIP_BASE), "zip", root_dir=RESULT.parent, base_dir=RESULT.name
)
print("RESULT_ZIP", zip_path, flush=True)
shutil.rmtree(RUNTIME)
'''


def _run_error_details(run_id: str) -> str:
    try:
        run = _request_json(
            "GET",
            f"/runs/{run_id}?snapshotDelivery=blocks",
        )["run"]
    except Exception as exc:
        return f"could not fetch run snapshot: {exc}"
    chunks: list[str] = []
    for block in run.get("snapshotBlocks") or []:
        for output in block.get("outputs") or []:
            if output.get("text"):
                chunks.append(str(output["text"]))
            if output.get("evalue"):
                chunks.append(
                    f"{output.get('ename', 'Error')}: {output['evalue']}"
                )
    details = "\n".join(chunks).strip()
    return details[-4000:] if details else "no Deepnote error output available"


def _safe_extract_zip(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (destination / info.filename).resolve()
            if not target.is_relative_to(destination):
                raise RuntimeError(f"unsafe path in Deepnote result ZIP: {info.filename}")
        zf.extractall(destination)


def _fail_for_retry(coordinator: Coordinator, job_id: str, reason: str) -> None:
    try:
        coordinator.fail_attempt(job_id, reason[:4000], retry=True)
    except Exception:
        pass


def _create_run_serialized(
    *,
    notebook_id: str,
    block_id: str,
    content: str,
) -> dict:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
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
        fcntl.flock(lock_file, fcntl.LOCK_UN)
    return created


def dispatch_one(
    *,
    poll_seconds: int = 10,
    timeout_seconds: int = 14400,
    keep_remote: bool = False,
) -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    project_id = cfg["project_id"]
    notebook_id = cfg["notebook_id"]
    block_id = cfg["block_id"]

    repo_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        text=True,
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
    remote_dir = f"cgm-worker/jobs/{attempt_tag}"
    remote_zip = f"cgm-worker/results/{attempt_tag}.zip"
    uploaded_paths: list[str] = []
    run_id: str | None = None

    try:
        if export_dir.exists():
            shutil.rmtree(export_dir)
        coordinator.export_job(lease.job_id, export_dir)

        for local in sorted(export_dir.iterdir()):
            if local.is_file():
                uploaded_paths.append(
                    _upload_file(
                        project_id,
                        f"{remote_dir}/{local.name}",
                        local,
                    )
                )

        content = _remote_code(
            remote_dir,
            remote_zip,
            repo_sha,
            attempt_tag,
        )
        created = _create_run_serialized(
            notebook_id=notebook_id,
            block_id=block_id,
            content=content,
        )
        run_id = created["runId"]
        coordinator.mark_running(lease.job_id)
        print("RUN", run_id, lease.job_id, flush=True)

        deadline = time.monotonic() + timeout_seconds
        status = created["status"]
        while status not in {"success", "error", "internal_error", "stopped"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Deepnote run {run_id} timed out")
            time.sleep(poll_seconds)
            status = _request_json("GET", f"/runs/{run_id}")["run"]["status"]
            print("STATUS", status, flush=True)

        if status != "success":
            details = _run_error_details(run_id)
            raise RuntimeError(
                f"Deepnote run {run_id} ended {status}: {details}"
            )

        local_zip = IMPORTS / f"{attempt_tag}.zip"
        _download_file(project_id, remote_zip, local_zip)

        result_root = IMPORTS / attempt_tag
        if result_root.exists():
            shutil.rmtree(result_root)
        result_root.mkdir(parents=True)
        _safe_extract_zip(local_zip, result_root)

        candidates = list(result_root.rglob("job-result.json"))
        if len(candidates) != 1:
            raise RuntimeError(
                f"expected one Deepnote job-result.json, got {candidates}"
            )
        receipt = coordinator.import_result(candidates[0].parent)
    except Exception as exc:
        _fail_for_retry(
            coordinator,
            lease.job_id,
            f"Deepnote dispatch: {exc}",
        )
        raise

    if not keep_remote:
        for remote_path in uploaded_paths:
            _delete_file(project_id, remote_path)
        _delete_file(project_id, remote_zip)

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
    parser.add_argument(
        "--keep-remote",
        action="store_true",
        help="keep uploaded Deepnote job/result files after success",
    )
    args = parser.parse_args(argv)
    return dispatch_one(
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        keep_remote=args.keep_remote,
    )


if __name__ == "__main__":
    raise SystemExit(main())
