#!/usr/bin/env python3
from __future__ import annotations
import json, os
from pathlib import Path

from chessgrandmaster.coordinator import Coordinator
from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge
from chessgrandmaster.coordinator.s3_mailbox import S3MailboxStorage

CGM = Path("/home/ubuntu/data/cgm")

def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def main() -> int:
    storage = S3MailboxStorage(
        endpoint_url=required("CGM_MOLAB_S3_ENDPOINT"), bucket=required("CGM_MOLAB_S3_BUCKET"),
        prefix=os.environ.get("CGM_MOLAB_S3_PREFIX", "gm-analyzer/molab"),
        access_key=required("CGM_MOLAB_S3_ACCESS_KEY"), secret_key=required("CGM_MOLAB_S3_SECRET_KEY"),
    )
    coordinator = Coordinator(CGM / "coordinator.sqlite", archive_root=CGM / "archive")
    bridge = MolabS3Bridge(
        coordinator=coordinator, storage=storage, root=CGM / "molab-s3-bridge",
        max_jobs=int(os.environ.get("CGM_MOLAB_MAX_JOBS", "45")), min_priority=500,
    )
    processed = bridge.poll_once()
    current = bridge.ensure_current_batch()
    payload = {
        "processed": None if processed is None else processed.__dict__,
        "current": None if current is None else current.__dict__,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
