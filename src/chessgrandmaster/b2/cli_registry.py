"""``cgm-registry``: read-only B2 registry inspection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .registry import Registry


DEFAULT_REGISTRY_PATH = ".cgm/registry.sqlite"
_PROGRAM = "cgm-registry"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROGRAM,
        description=(
            "Inspect the B2 acquisition registry. Read-only: prints one JSON "
            "object on stdout and never mutates registry state."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser(
        "status",
        help="Print a global summary, or one tournament when ID is given.",
    )
    status.add_argument(
        "tournament_id",
        nargs="?",
        type=int,
        default=None,
        help="Optional tournament id for a per-tournament summary.",
    )
    status.add_argument(
        "--registry",
        default=DEFAULT_REGISTRY_PATH,
        help=f"registry.sqlite path (default: {DEFAULT_REGISTRY_PATH}).",
    )
    return parser


def _registry_for_read(registry_argument: str) -> Path:
    path = Path(registry_argument).expanduser().resolve()
    if path.is_dir():
        raise ValueError("registry path must be a file, not a directory")
    if not path.is_file():
        raise FileNotFoundError(f"registry does not exist: {path}")
    return path


def _global_payload(registry: Registry) -> dict[str, object]:
    return {
        "ok": True,
        "registry": str(registry.path),
        "counts": registry.registry_counts(),
        "tournament_status_counts": registry.tournament_status_counts(),
        "tournaments": registry.list_tournaments(),
    }


def _tournament_payload(registry: Registry, tournament_id: int) -> dict[str, object]:
    summary = registry.get_tournament_summary(tournament_id)
    return {
        "ok": True,
        "registry": str(registry.path),
        **summary,
    }


def _emit(payload: dict[str, object], stream) -> None:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
    stream.write("\n")
    stream.flush()


def _emit_error(message: str, error_type: str) -> None:
    _emit(
        {"ok": False, "error": {"type": error_type, "message": message}},
        stream=sys.stdout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(arguments)

    if args.command != "status":
        _emit_error(f"unknown command: {args.command}", "usage")
        return 2

    try:
        path = _registry_for_read(args.registry)
        registry = Registry.open_read_only(path)
        if args.tournament_id is None:
            payload = _global_payload(registry)
        else:
            payload = _tournament_payload(registry, int(args.tournament_id))
    except (KeyError, FileNotFoundError) as exc:
        _emit_error(" ".join(str(exc).split()), "not_found")
        return 1
    except KeyboardInterrupt:
        _emit_error("interrupted", "interrupted")
        return 130
    except Exception as exc:
        _emit_error(" ".join(str(exc).split()) or type(exc).__name__, type(exc).__name__)
        return 1

    _emit(payload, stream=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
