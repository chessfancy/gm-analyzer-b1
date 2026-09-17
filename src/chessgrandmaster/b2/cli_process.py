"""``cgm-process``: process downloaded immutable B2 PGNs locally."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .processor import CorpusProcessor
from .registry import Registry
from .storage import LocalObjectStore


PROGRAM = "cgm-process"


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog=PROGRAM,
        description=(
            "Process downloaded immutable B2 PGNs locally without network access. "
            "Prints one JSON object on stdout."
        ),
    )
    parser.add_argument("--registry", required=True, help="registry.sqlite path")
    parser.add_argument("--root", required=True, help="B2 local root")
    parser.add_argument(
        "--workers",
        default="auto",
        help="auto or a positive worker count (default: auto)",
    )
    parser.add_argument(
        "--game-timeout-sec",
        type=float,
        default=2.0,
        help="hard timeout after worker start for one game (default: 2.0)",
    )
    parser.add_argument("--report", help="optional JSON report path")
    return parser


def _resolve_layout(registry_argument: str, root_argument: str) -> tuple[Path, Path, Path]:
    registry_path = Path(registry_argument).expanduser().resolve()
    root_path = Path(root_argument).expanduser().resolve()
    if registry_path.is_dir():
        raise ValueError("registry path must be a file, not a directory")
    if root_path.is_file():
        raise ValueError("root path must be a directory, not a file")
    root_path.mkdir(parents=True, exist_ok=True)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    return registry_path, root_path / "objects", root_path / "workspace"


def _error_payload(error: BaseException) -> dict[str, object]:
    message = " ".join(str(error).split())
    return {
        "ok": False,
        "error": {
            "type": type(error).__name__,
            "message": message or type(error).__name__,
        },
    }


def _emit(payload: dict[str, object], stream) -> None:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
    stream.write("\n")
    stream.flush()


def _write_report(path: str, payload: dict[str, object]) -> None:
    report_path = Path(path).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    except Exception as exc:
        _emit(_error_payload(exc), sys.stdout)
        return 2

    try:
        registry_path, objects_path, workspace_path = _resolve_layout(
            args.registry,
            args.root,
        )
        registry = Registry.open_read_only(registry_path)
        # Processing writes registry rows, so open the normal controlled writer
        # handle after validating that the target database exists and is v1.
        registry = Registry(registry_path)
        processor = CorpusProcessor(
            registry=registry,
            store=LocalObjectStore(objects_path),
            workspace=workspace_path,
            workers=args.workers,
            game_timeout_sec=args.game_timeout_sec,
        )
        report = processor.process()
        payload = report.to_dict()
        if args.report:
            _write_report(args.report, payload)
    except KeyboardInterrupt:
        _emit(_error_payload(RuntimeError("processing interrupted")), sys.stdout)
        return 130
    except Exception as exc:
        _emit(_error_payload(exc), sys.stdout)
        return 2

    _emit(payload, sys.stdout)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
