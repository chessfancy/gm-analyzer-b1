#!/usr/bin/env python3
"""Poll Deepnote project storage, import ready results, and publish the next manual batch."""

from __future__ import annotations

import argparse
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
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def download(self, path: str, destination: Path) -> None:
        dn._download_file(self.project_id, path, destination)

    def upload(self, path: str, source: Path) -> None:
        last_uploaded = None
        for attempt in range(4):
            dn._delete_file(self.project_id, path)
            if attempt:
                time.sleep(float(attempt))
            uploaded = dn._upload_file(self.project_id, path, source)
            if uploaded == path:
                return
            last_uploaded = uploaded
            # Deepnote can auto-rename when deletion has not propagated yet.
            # Remove the collision artifact and retry the requested identity.
            dn._delete_file(self.project_id, uploaded)
            time.sleep(float(attempt + 1))
        raise RuntimeError(
            f"Deepnote uploaded unexpected path after retries: {last_uploaded!r}"
        )

    def delete(self, path: str) -> None:
        dn._delete_file(self.project_id, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator-db", type=Path, default=DB)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    parser.add_argument("--bridge-root", type=Path, default=BRIDGE_ROOT)
    parser.add_argument("--max-jobs", type=int, default=8)
    parser.add_argument("--min-priority", type=int, default=500)
    parser.add_argument("--max-plies", type=int, default=None)
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
    payload = {
        "ok": processed is None or processed.rejected == 0,
        "processed": None if processed is None else {
            "batch_id": processed.batch_id,
            "completed": processed.completed,
            "rejected": processed.rejected,
            "next_batch_id": processed.next_batch_id,
        },
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
