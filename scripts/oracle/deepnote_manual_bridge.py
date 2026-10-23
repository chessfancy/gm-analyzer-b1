#!/usr/bin/env python3
"""Poll Deepnote project storage, import ready results, and publish the next manual batch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import importlib.util
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from chessgrandmaster.coordinator import Coordinator
from chessgrandmaster.coordinator.deepnote_manual_bridge import DeepnoteManualBridge

HOME = Path("/home/ubuntu")
CGM = HOME / "data/cgm"
REPO = Path(__file__).resolve().parents[2]
DB = Path("/home/ubuntu/data/cgm/coordinator.sqlite")
ARCHIVE_ROOT = CGM / "archive"
BRIDGE_ROOT = CGM / "deepnote-manual-bridge"
CONFIG = Path("/home/ubuntu/.config/cgm/deepnote-worker.json")
SECRET = Path("/home/ubuntu/.config/cgm/secrets/deepnote_api_key")
DISPATCH = REPO / "scripts/oracle/deepnote_dispatch.py"
BASE = "https://api.deepnote.com/v2"
AUTOTRIGGER_STATE = BRIDGE_ROOT / "autotrigger.json"
ACTIVE_RUN_STATES = {"pending", "running"}


def _with_429_retry(operation, *, label: str, max_retries: int = 3):
    """Retry a Deepnote API operation on HTTP 429, respecting Retry-After.

    Long server cooldowns are deferred to the next scheduled bridge poll rather
    than holding the cron lock for several minutes.
    """
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt >= max_retries:
                raise
            raw = exc.headers.get("Retry-After") if exc.headers else None
            delay = None
            if raw:
                try:
                    delay = float(raw)
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(raw)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        delay = None
            if delay is None:
                delay = min(60.0, 5.0 * (2 ** attempt))
            if delay > 180:
                print(
                    f"Deepnote API HTTP 429 during {label}; Retry-After={delay:.0f}s "
                    "exceeds in-run wait budget; defer to next bridge poll.",
                    flush=True,
                )
                raise
            delay = max(1.0, delay)
            print(
                f"Deepnote API HTTP 429 during {label}; retry {attempt + 1}/"
                f"{max_retries} after {delay:.0f}s.",
                flush=True,
            )
            time.sleep(delay)

_spec = importlib.util.spec_from_file_location("cgm_deepnote_dispatch", DISPATCH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"cannot import {DISPATCH}")
dn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dn)


class DeepnoteProjectStorage:
    def __init__(self, project_id: str) -> None:
        self.project_id = str(project_id)

    def read_bytes(self, path: str) -> bytes | None:
        query = urllib.parse.urlencode({"projectId": self.project_id, "path": path})
        request = urllib.request.Request(
            BASE + "/files/download?" + query,
            headers={
                "Authorization": f"Bearer {SECRET.read_text(encoding='utf-8').strip()}",
                "Accept": "application/octet-stream",
                "User-Agent": "ChessGrandmaster coordinator/1.0",
            },
        )
        def _read():
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()

        try:
            return _with_429_retry(_read, label=f"download {path}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def download(self, path: str, destination: Path) -> None:
        dn._download_file(self.project_id, path, destination)

    def upload(self, path: str, source: Path) -> None:
        last_uploaded = None
        for attempt in range(4):
            _with_429_retry(
                lambda: dn._delete_file(self.project_id, path), label=f"delete {path}"
            )
            if attempt:
                time.sleep(float(attempt))
            uploaded = _with_429_retry(
                lambda: dn._upload_file(self.project_id, path, source),
                label=f"upload {path}",
            )
            if uploaded == path:
                return
            last_uploaded = uploaded
            # Deepnote can auto-rename when deletion has not propagated yet.
            # Remove the collision artifact and retry the requested identity.
            _with_429_retry(
                lambda: dn._delete_file(self.project_id, uploaded),
                label=f"delete collision {uploaded}",
            )
            time.sleep(float(attempt + 1))
        raise RuntimeError(
            f"Deepnote uploaded unexpected path after retries: {last_uploaded!r}"
        )

    def delete(self, path: str) -> None:
        dn._delete_file(self.project_id, path)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _find_analyze_block_id(notebook: dict) -> str:
    blocks = notebook.get("blocks") or []
    matches = [
        str(block.get("id") or "")
        for block in blocks
        if isinstance(block, dict)
        and "run_deepnote_manual_cycle.py" in str(block.get("content") or "")
    ]
    matches = [value for value in matches if value]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one Deepnote ANALYZE block, got {matches}")
    return matches[0]


def maybe_auto_trigger(*, current, storage, config: dict, state_path: Path = AUTOTRIGGER_STATE) -> dict:
    if current is None:
        return {"triggered": False, "reason": "no-current-batch"}

    batch_id = str(current.batch_id)
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    same_batch = str(previous.get("batch_id") or "") == batch_id

    # A completed progress marker is authoritative even when autotrigger.json
    # still points at this batch. Check it before deduplicating by batch ID.
    progress_path = f"cgm-manual/runtime/{batch_id}/progress.json"
    progress_bytes = storage.read_bytes(progress_path)
    if progress_bytes is not None:
        progress = json.loads(progress_bytes.decode("utf-8"))
        if str(progress.get("state") or "") == "complete":
            return {"triggered": False, "batch_id": batch_id, "reason": "already-complete"}

    notebook_id = str(config["notebook_id"])
    history = dn._request_json("GET", f"/notebooks/{notebook_id}/runs?pageSize=20")
    runs = history.get("runs") or []
    for run in runs:
        if str(run.get("status") or "") in ACTIVE_RUN_STATES:
            return {
                "triggered": False,
                "batch_id": batch_id,
                "reason": "run-active",
                "run_id": run.get("runId"),
            }

    retry_count = int(previous.get("retry_count", 0)) if same_batch else 0
    if same_batch:
        previous_run_id = str(previous.get("run_id") or "")
        previous_run = next(
            (run for run in runs if str(run.get("runId") or "") == previous_run_id),
            None,
        )
        if previous_run is None:
            return {
                "triggered": False,
                "batch_id": batch_id,
                "reason": "previous-run-not-found-review-required",
                "run_id": previous_run_id or None,
            }
        previous_status = str(previous_run.get("status") or "").lower()
        if previous_status not in {"error", "failed", "cancelled", "canceled"}:
            return {
                "triggered": False,
                "batch_id": batch_id,
                "reason": "previous-run-terminal-incomplete-review-required",
                "run_id": previous_run_id,
                "status": previous_status or None,
            }
        # Allow one automatic recovery run for a terminal failure, but do not
        # create a five-minute retry storm if the recovery run fails as well.
        if retry_count >= 1:
            return {
                "triggered": False,
                "batch_id": batch_id,
                "reason": "automatic-retry-limit-reached-review-required",
                "run_id": previous_run_id,
                "status": previous_status,
            }
        retry_count += 1

    notebook = dn._request_json("GET", f"/notebooks/{notebook_id}")["notebook"]
    block_id = _find_analyze_block_id(notebook)
    created = dn._request_json("POST", "/runs", {
        "notebookId": notebook_id,
        "detached": False,
        "blockIds": [block_id],
    })
    result = {
        "batch_id": batch_id,
        "run_id": str(created["runId"]),
        "status": str(created["status"]),
        "retry_count": retry_count,
        "triggered_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json_atomic(state_path, result)
    return {
        "triggered": True,
        "batch_id": batch_id,
        "run_id": result["run_id"],
        "status": result["status"],
        "retry_count": retry_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator-db", type=Path, default=DB)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--bridge-root", type=Path, default=BRIDGE_ROOT)
    parser.add_argument("--max-jobs", type=int, default=8)
    parser.add_argument("--min-priority", type=int, default=500)
    parser.add_argument("--max-plies", type=int, default=None)
    parser.add_argument("--auto-trigger", action="store_true")
    parser.add_argument("--trigger-state", type=Path, default=AUTOTRIGGER_STATE)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    coordinator = Coordinator(args.coordinator_db, archive_root=args.archive_root)
    storage = DeepnoteProjectStorage(config["project_id"])
    bridge = DeepnoteManualBridge(
        coordinator=coordinator,
        storage=storage,
        root=args.bridge_root,
        max_jobs=args.max_jobs,
        min_priority=args.min_priority,
        max_plies=args.max_plies,
    )
    processed = bridge.poll_once()
    current = bridge.ensure_current_batch()
    auto_trigger = None
    if args.auto_trigger:
        auto_trigger = maybe_auto_trigger(
            current=current, storage=storage, config=config, state_path=args.trigger_state
        )
    payload = {
        "ok": processed is None or processed.rejected == 0,
        "processed": None if processed is None else {
            "batch_id": processed.batch_id,
            "completed": processed.completed,
            "rejected": processed.rejected,
            "next_batch_id": processed.next_batch_id,
        },
        "auto_trigger": auto_trigger,
        "current": None if current is None else {
            "batch_id": current.batch_id,
            "jobs": current.jobs,
            "games": current.games,
            "plies": current.plies,
            "sha256": current.sha256,
        },
    }
    print(json.dumps(payload, sort_keys=True))
    return 1 if processed is not None and processed.rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
