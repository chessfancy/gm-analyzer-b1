#!/usr/bin/env python3
"""Top up the Oracle coordinator from canonical registry tournaments."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.coordinator import Coordinator
from chessgrandmaster.coordinator.refill import refill_backfill_queue

CGM = Path("/home/ubuntu/data/cgm")
REGISTRY = CGM / "corpus-2026/registry.sqlite"
DB = CGM / "coordinator.sqlite"
ARCHIVE = CGM / "archive"
PACKAGES = CGM / "packages"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--coordinator-db", type=Path, default=DB)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE)
    parser.add_argument("--package-root", type=Path, default=PACKAGES)
    parser.add_argument("--source", default="chess-results")
    parser.add_argument("--low-watermark", type=int, default=8)
    parser.add_argument("--high-watermark", type=int, default=24)
    parser.add_argument("--target-plies", type=int, default=1800)
    parser.add_argument("--priority", type=int, default=500)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    summary = refill_backfill_queue(
        registry=Registry(args.registry),
        coordinator=Coordinator(args.coordinator_db, archive_root=args.archive_root),
        package_root=args.package_root,
        source_name=args.source,
        low_watermark=args.low_watermark,
        high_watermark=args.high_watermark,
        target_plies=args.target_plies,
        priority=args.priority,
    )
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
