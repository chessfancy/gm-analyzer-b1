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
REPO = HOME / "projects/gm-analyzer-b1"
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


def _wait_run(run_id: str, poll_seconds: int, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    terminal = {"success", "error", "internal_error", "stopped"}
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Deepnote session run timed out: {run_id}")
        run = dn._request_json("GET", f"/runs/{run_id}")["run"]
        status = str(run.get("status", "")).lower()
        print(
            json.dumps(
                {
                    "event": "session_run_status",
                    "run_id": run_id,
                    "status": status,
                    "checked_at": time.time(),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if status in terminal:
            return run
        time.sleep(poll_seconds)


def _stop_session(session_id: str) -> None:
    try:
        dn._request_json("DELETE", f"/sessions/{session_id}")
    except Exception as exc:
        print(f"SESSION_STOP_WARNING {exc}", flush=True)


def dispatch_test_job(
    *,
    poll_seconds: int = 60,
    timeout_seconds: int = 172800,
) -> int:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    project_id = cfg["project_id"]
    notebook_id = cfg["notebook_id"]
    source_block_id = cfg["block_id"]

    repo_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        text=True,
    ).strip()

    coordinator = Coordinator(COORD_DB, archive_root=ARCHIVE)
    lease = coordinator.lease_next(
        "deepnote-session-api",
        "deepnote",
        lease_seconds=timeout_seconds + 3600,
    )
    if lease is None:
        print(json.dumps({"ok": True, "message": "no pending session test job"}))
        return 0

    attempt_tag = f"session-attempt-{lease.attempt_id}-{uuid.uuid4().hex[:8]}"
    export_dir = EXPORTS / attempt_tag
    remote_dir = f"cgm-session-test/jobs/{attempt_tag}"
    remote_zip = f"cgm-session-test/results/{attempt_tag}.zip"
    local_zip = IMPORTS / f"{attempt_tag}.zip"
    extract_dir = IMPORTS / attempt_tag
    uploaded_paths: list[str] = []
    session_id: str | None = None

    try:
        if export_dir.exists():
            shutil.rmtree(export_dir)
        coordinator.export_job(lease.job_id, export_dir)

        for source in sorted(export_dir.iterdir()):
            if source.is_file():
                remote_path = f"{remote_dir}/{source.name}"
                uploaded = dn._upload_file(project_id, remote_path, source)
                uploaded_paths.append(uploaded)

        # Create the session from a harmless source notebook. The session gets
        # its own notebook copy, so the long-running worker code is patched
        # only into that copy. The source notebook remains idle afterwards.
        dn._request_json(
            "PATCH",
            f"/blocks/{source_block_id}",
            {"content": "print('CGM session bootstrap')"},
        )
        try:
            created = dn._request_json(
                "POST",
                "/sessions",
                {
                    "notebookId": notebook_id,
                    "storageMode": "read_write",
                },
            )
        finally:
            dn._request_json(
                "PATCH",
                f"/blocks/{source_block_id}",
                {"content": "print('CGM session source idle')"},
            )

        session_id, source_notebook_id, session_notebook_id = _session_fields(created)
        session_notebook = dn._request_json(
            "GET",
            f"/notebooks/{session_notebook_id}",
        )["notebook"]
        session_block_id = _choose_session_block(session_notebook)

        worker_code = dn._remote_code(
            remote_dir,
            remote_zip,
            repo_sha,
            attempt_tag,
        )
        dn._request_json(
            "PATCH",
            f"/blocks/{session_block_id}",
            {"content": worker_code},
        )
        submitted = dn._request_json(
            "POST",
            f"/sessions/{session_id}/runs",
            {
                "notebookId": source_notebook_id,
                "blockIds": [session_block_id],
            },
        )
        run_id = str(submitted["runId"])
        coordinator.mark_running(lease.job_id)

        print(
            json.dumps(
                {
                    "event": "session_started",
                    "job_id": lease.job_id,
                    "attempt": lease.attempt_number,
                    "session_id": session_id,
                    "session_notebook_id": session_notebook_id,
                    "run_id": run_id,
                    "repo_sha": repo_sha,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        run = _wait_run(run_id, poll_seconds, timeout_seconds)
        if str(run.get("status", "")).lower() != "success":
            details = dn._run_error_details(run_id)
            raise RuntimeError(
                f"Deepnote session run ended {run.get('status')}: {details}"
            )

        if local_zip.exists():
            local_zip.unlink()
        dn._download_file(project_id, remote_zip, local_zip)

        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True)
        dn._safe_extract_zip(local_zip, extract_dir)
        candidates = list(extract_dir.rglob("job-result.json"))
        if len(candidates) != 1:
            raise RuntimeError(
                f"expected one session job-result.json, got {candidates}"
            )

        receipt = coordinator.import_result(candidates[0].parent)
        print(
            json.dumps(
                {
                    "ok": True,
                    "provider": "deepnote-session",
                    "job_id": receipt.job_id,
                    "attempt": receipt.attempt_number,
                    "state": receipt.state.value,
                    "archive": str(receipt.archive_path),
                    "session_id": session_id,
                    "run_id": run_id,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    except Exception as exc:
        try:
            coordinator.fail_attempt(
                lease.job_id,
                f"Deepnote session dispatch: {exc}"[:4000],
                retry=False,
            )
        except Exception:
            pass
        raise
    finally:
        # Stop the interactive machine only after result verification/import,
        # or after an explicit failure. No session is intentionally left alive.
        if session_id:
            _stop_session(session_id)
        for remote_path in uploaded_paths:
            dn._delete_file(project_id, remote_path)
        dn._delete_file(project_id, remote_zip)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=int, default=172800)
    args = parser.parse_args(argv)
    return dispatch_test_job(
        poll_seconds=args.poll_seconds,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
