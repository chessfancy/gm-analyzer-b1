#!/usr/bin/env python3
"""Run one immutable CGM job through Deepnote's interactive Sessions API."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import time
import urllib.parse
import uuid
import zipfile

from chessgrandmaster.coordinator import Coordinator


HOME = Path.home()
REPO = Path(__file__).resolve().parents[2]
TEST_ROOT = HOME / "data/cgm/deepnote-session-test"
COORD_DB = TEST_ROOT / "coordinator.sqlite"
ARCHIVE = TEST_ROOT / "archive"
EXPORTS = TEST_ROOT / "exports"
IMPORTS = TEST_ROOT / "imports"
CONFIG = HOME / ".config/cgm/deepnote-worker.json"
DISPATCH = REPO / "scripts/oracle/deepnote_dispatch.py"

_spec = importlib.util.spec_from_file_location("cgm_deepnote_detached", DISPATCH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"cannot import {DISPATCH}")
dn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dn)


def _session_fields(created: dict) -> tuple[str, str, str]:
    session = created.get("session") or {}
    session_id = str(session.get("id") or session.get("sessionId") or "")
    source_notebook_id = str(
        session.get("notebookId") or session.get("sourceNotebookId") or ""
    )
    session_notebook_id = str(session.get("sessionNotebookId") or "")
    if not session_id or not source_notebook_id or not session_notebook_id:
        raise RuntimeError(f"unexpected session response: {created}")
    return session_id, source_notebook_id, session_notebook_id


def _choose_session_block(notebook: dict) -> str:
    blocks = notebook.get("blocks") or []
    code_blocks = [
        block for block in blocks
        if isinstance(block, dict) and str(block.get("type", "")).lower() == "code"
    ]
    candidates = code_blocks or [b for b in blocks if isinstance(b, dict)]
    if not candidates:
        raise RuntimeError("session notebook contains no blocks")
    block_id = str(candidates[0].get("id") or "")
    if not block_id:
        raise RuntimeError("session notebook block has no id")
    return block_id


def _request_session_json(method: str, path: str, payload=None, *, timeout: int = 60):
    return dn._request_json(method, path, payload, timeout=timeout)


def _session_remote_code(
    *,
    job_dir: str,
    result_zip: str,
    repo_sha: str,
    session_tag: str,
) -> str:
    return dn._remote_code(job_dir, result_zip, repo_sha, session_tag)


def run_session_once(
    *,
    poll_seconds: int = 10,
    timeout_seconds: int = 14400,
    keep_remote: bool = False,
    min_priority: int | None = None,
    max_plies: int | None = None,
) -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    project_id = cfg["project_id"]
    source_notebook_id = cfg["notebook_id"]

    repo_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        text=True,
    ).strip()

    coordinator = Coordinator(COORD_DB, archive_root=ARCHIVE)
    lease = coordinator.lease_next(
        "deepnote-session-api",
        "deepnote-session-api",
        lease_seconds=timeout_seconds + 1800,
        min_priority=min_priority,
        max_plies=max_plies,
    )
    if lease is None:
        print(json.dumps({"ok": True, "message": "no pending job"}))
        return 0

    session_tag = f"session-{lease.attempt_id}-{uuid.uuid4().hex[:8]}"
    export_dir = EXPORTS / session_tag
    remote_dir = f"cgm-session/jobs/{session_tag}"
    remote_zip = f"cgm-session/results/{session_tag}.zip"
    uploaded_paths: list[str] = []
    session_id: str | None = None

    try:
        if export_dir.exists():
            shutil.rmtree(export_dir)
        coordinator.export_job(lease.job_id, export_dir)
        for local in sorted(export_dir.iterdir()):
            if local.is_file():
                uploaded_paths.append(
                    dn._upload_file(project_id, f"{remote_dir}/{local.name}", local)
                )

        created = _request_session_json(
            "POST",
            "/sessions",
            {"notebookId": source_notebook_id},
        )
        session_id, returned_source, session_notebook_id = _session_fields(created)
        if returned_source != source_notebook_id:
            raise RuntimeError("Deepnote session source notebook identity mismatch")
        session_notebook = _request_session_json(
            "GET", f"/notebooks/{session_notebook_id}"
        )["notebook"]
        block_id = _choose_session_block(session_notebook)
        code = _session_remote_code(
            job_dir=remote_dir,
            result_zip=remote_zip,
            repo_sha=repo_sha,
            session_tag=session_tag,
        )
        _request_session_json("PATCH", f"/blocks/{block_id}", {"content": code})
        started = _request_session_json(
            "POST",
            f"/sessions/{session_id}/runs",
            {"blockId": block_id},
        )
        run_id = str(started.get("runId") or started.get("run", {}).get("id") or "")
        if not run_id:
            raise RuntimeError(f"Deepnote session run has no run ID: {started}")
        coordinator.mark_running(lease.job_id)
        print("SESSION", session_id, "RUN", run_id, lease.job_id, flush=True)

        deadline = time.monotonic() + timeout_seconds
        status = ""
        while status not in {"success", "error", "internal_error", "stopped"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Deepnote session run {run_id} timed out")
            time.sleep(poll_seconds)
            response = _request_session_json("GET", f"/runs/{run_id}")
            status = str(response["run"]["status"])
            print("STATUS", status, flush=True)
        if status != "success":
            raise RuntimeError(f"Deepnote session run {run_id} ended {status}")

        local_zip = IMPORTS / f"{session_tag}.zip"
        dn._download_file(project_id, remote_zip, local_zip)
        result_root = IMPORTS / session_tag
        if result_root.exists():
            shutil.rmtree(result_root)
        result_root.mkdir(parents=True)
        dn._safe_extract_zip(local_zip, result_root)
        candidates = list(result_root.rglob("job-result.json"))
        if len(candidates) != 1:
            raise RuntimeError(
                f"expected one Deepnote session job-result.json, got {candidates}"
            )
        receipt = coordinator.import_result(candidates[0].parent)
    except Exception as exc:
        dn._fail_for_retry(
            coordinator,
            lease.job_id,
            f"Deepnote session: {exc}",
        )
        raise

    if not keep_remote:
        for remote_path in uploaded_paths:
            dn._delete_file(project_id, remote_path)
        dn._delete_file(project_id, remote_zip)

    print(json.dumps({
        "ok": True,
        "provider": "deepnote-session-api",
        "job_id": receipt.job_id,
        "attempt": receipt.attempt_number,
        "state": receipt.state.value,
        "session_id": session_id,
    }, sort_keys=True))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=int, default=14400)
    parser.add_argument("--min-priority", type=int, default=None)
    parser.add_argument("--max-plies", type=int, default=None)
    parser.add_argument("--keep-remote", action="store_true")
    args = parser.parse_args(argv)
    return run_session_once(
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
        keep_remote=args.keep_remote,
        min_priority=args.min_priority,
        max_plies=args.max_plies,
    )


if __name__ == "__main__":
    raise SystemExit(main())
