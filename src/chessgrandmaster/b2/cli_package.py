"""Command-line entry point for deterministic local B2b packaging."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .packaging import PackageService
from .registry import Registry
from .s3_storage import S3ObjectStore
from .storage import LocalObjectStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cgm-package",
        description="Package a canonical B2 revision into portable B1 jobs.",
    )
    parser.add_argument("tournament_id")
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--revision", type=int, default=None)
    parser.add_argument("--target-plies", type=int, default=3000)
    parser.add_argument("--store", choices=("local", "s3"), default="local")
    parser.add_argument("--s3-bucket", default=None)
    parser.add_argument("--s3-prefix", default="")
    parser.add_argument("--s3-endpoint-url", default=None)
    return parser


def _workspace_for(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    return resolved.parent / f".{resolved.name}.cgm-package-work"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = Registry(args.registry)
        if args.store == "local":
            store = LocalObjectStore(args.root)
        else:
            if not args.s3_bucket:
                raise ValueError("--s3-bucket is required with --store s3")
            store = S3ObjectStore(
                args.s3_bucket,
                prefix=args.s3_prefix,
                endpoint_url=args.s3_endpoint_url,
            )
        result = PackageService(
            registry,
            store,
            _workspace_for(args.root),
        ).package(
            args.tournament_id,
            revision_number=args.revision,
            target_plies=args.target_plies,
        )
    except Exception as exc:
        print(f"cgm-package: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            result.to_summary(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
