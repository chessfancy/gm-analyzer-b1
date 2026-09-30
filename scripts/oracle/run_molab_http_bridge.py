#!/usr/bin/env python3
from __future__ import annotations
import json, os
from pathlib import Path
from chessgrandmaster.coordinator import Coordinator
from chessgrandmaster.coordinator.http_mailbox import HTTPMailboxStorage
from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge
CGM = Path("/home/ubuntu/data/cgm")
def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value: raise RuntimeError(f"missing required environment variable: {name}")
    return value
def main() -> int:
    storage = HTTPMailboxStorage(
        base_url=required("CGM_MOLAB_RELAY_URL"),
        token=os.environ.get("CGM_MOLAB_RELAY_ADMIN_TOKEN", "").strip(),
        timeout=300,
    )
    bridge = MolabS3Bridge(coordinator=Coordinator(CGM / "coordinator.sqlite", archive_root=CGM / "archive"), storage=storage,
                          root=CGM / "molab-http-bridge", max_jobs=int(os.environ.get("CGM_MOLAB_MAX_JOBS", "45")), min_priority=500)
    processed = bridge.poll_once(); current = bridge.ensure_current_batch()
    print(json.dumps({"processed": None if processed is None else processed.__dict__, "current": None if current is None else current.__dict__}, sort_keys=True)); return 0
if __name__ == "__main__": raise SystemExit(main())
