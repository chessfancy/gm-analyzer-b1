#!/usr/bin/env python3
"""Create and import coordinator-native manual worker batches."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from chessgrandmaster.coordinator import Coordinator
from chessgrandmaster.coordinator.manual_batch import (
    archive_directory,
    create_manual_batch,
    import_manual_result_archive,
)

CGM = Path("/home/ubuntu/data/cgm")
DB = CGM / "coordinator.sqlite"
ARCHIVE_ROOT = CGM / "archive"
MANUAL_ROOT = CGM / "manual-batches"
IMPORT_ROOT = CGM / "imports/manual"
PUBLISH_ROOT = CGM / "distribution"
PUBLISHED_ARCHIVES = {
    "molab": "molab-batch.zip",
    "deepnote": "deepnote-batch.zip",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator-db", type=Path, default=DB)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="lease/export and publish a manual batch")
    create.add_argument("provider", choices=("molab", "deepnote"))
    create.add_argument("--max-jobs", type=int, required=True)
    create.add_argument("--min-priority", type=int, default=500)
    create.add_argument("--max-plies", type=int, default=None)
    create.add_argument("--lease-days", type=float, default=14.0)
    create.add_argument("--destination", type=Path, default=None)
    create.add_argument("--publish-root", type=Path, default=PUBLISH_ROOT)

    imp = sub.add_parser("import", help="verify and import a returned result archive")
    imp.add_argument("archive", type=Path)
    imp.add_argument("--extract-root", type=Path, default=IMPORT_ROOT)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    coordinator = Coordinator(args.coordinator_db, archive_root=args.archive_root)
    if args.command == "create":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = args.destination or MANUAL_ROOT / f"{args.provider}-{stamp}"
        summary = create_manual_batch(
            coordinator=coordinator,
            destination=destination,
            provider=args.provider,
            max_jobs=args.max_jobs,
            min_priority=args.min_priority,
            max_plies=args.max_plies,
            lease_seconds=args.lease_days * 24 * 3600,
        )
        args.publish_root.mkdir(parents=True, exist_ok=True)
        published = args.publish_root / PUBLISHED_ARCHIVES[args.provider]
        archive_directory(summary.path, published)
        sha = _sha256(published)
        published.with_suffix(published.suffix + ".sha256").write_text(
            f"{sha}  {published.name}\n", encoding="utf-8"
        )
        payload = asdict(summary)
        payload.update({"archive": str(published), "sha256": sha})
        payload["path"] = str(payload["path"])
        print(json.dumps(payload, sort_keys=True))
        return 0

    summary = import_manual_result_archive(
        coordinator=coordinator,
        archive_path=args.archive,
        extract_root=args.extract_root,
    )
    payload = asdict(summary)
    payload["extracted_path"] = str(payload["extracted_path"])
    print(json.dumps(payload, sort_keys=True))
    return 1 if summary.rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
