#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os
from pathlib import Path
from chessgrandmaster.coordinator.http_mailbox import HTTPMailboxStorage
from chessgrandmaster.coordinator.molab_s3_worker import run_molab_s3_cycle

def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value: raise RuntimeError(f"missing required environment variable: {name}")
    return value

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run current Oracle Molab HTTPS-relay batch.")
    parser.add_argument("--work-root", type=Path, default=Path(os.environ.get("CGM_MOLAB_WORK_ROOT", "cgm_molab_data/http-worker")))
    args = parser.parse_args(argv)
    storage = HTTPMailboxStorage(base_url=required("CGM_MOLAB_RELAY_URL"), token=required("CGM_MOLAB_RELAY_TOKEN"), timeout=300)
    summary = run_molab_s3_cycle(storage=storage, work_root=args.work_root)
    print(json.dumps(summary.__dict__, sort_keys=True)); return 0
if __name__ == "__main__": raise SystemExit(main())
