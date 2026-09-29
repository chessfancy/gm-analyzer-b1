#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os
from pathlib import Path

from chessgrandmaster.coordinator.molab_s3_worker import run_molab_s3_cycle
from chessgrandmaster.coordinator.s3_mailbox import S3MailboxStorage


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the current Oracle Molab S3 coordinator batch.")
    parser.add_argument("--work-root", type=Path, default=Path(os.environ.get("CGM_MOLAB_WORK_ROOT", "cgm_molab_data/s3-worker")))
    args = parser.parse_args(argv)
    storage = S3MailboxStorage(
        endpoint_url=required("CGM_MOLAB_S3_ENDPOINT"),
        bucket=required("CGM_MOLAB_S3_BUCKET"),
        prefix=os.environ.get("CGM_MOLAB_S3_PREFIX", "gm-analyzer/molab"),
        access_key=required("CGM_MOLAB_S3_ACCESS_KEY"),
        secret_key=required("CGM_MOLAB_S3_SECRET_KEY"),
    )
    summary = run_molab_s3_cycle(storage=storage, work_root=args.work_root)
    print(json.dumps(summary.__dict__, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
