#!/usr/bin/env python3
"""Run the current Deepnote manual batch and publish a ready pointer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chessgrandmaster.coordinator.deepnote_manual_bridge import READY_POINTER
from chessgrandmaster.coordinator.deepnote_manual_worker import run_deepnote_manual_cycle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=Path("/work"))
    args = parser.parse_args(argv)
    summary = run_deepnote_manual_cycle(work_root=args.work_root)
    print(json.dumps({
        "ok": True,
        "batch_id": summary.batch_id,
        "executed": summary.executed,
        "skipped": summary.skipped,
        "results": summary.results,
        "archive": str(summary.archive),
        "sha256": summary.sha256,
        "ready": str(Path(args.work_root) / READY_POINTER),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
