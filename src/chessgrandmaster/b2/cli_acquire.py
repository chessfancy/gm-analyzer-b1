"""``cgm-acquire``: acquire one B2 source into a canonical revision."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Sequence

from .acquisition import AcquisitionResult, AcquisitionService
from .registry import Registry
from .sources.base import SourceAdapter
from .sources.chesscom_broadcast import ChessComBroadcastAdapter
from .sources.chess_results import ChessResultsAdapter
from .sources.lichess_broadcast import LichessBroadcastAdapter
from .storage import LocalObjectStore
from .sources.twic import TwicAdapter


DEFAULT_REGISTRY_PATH = ".cgm/registry.sqlite"
DEFAULT_ROOT_PATH = ".cgm/b2"
_LAYOUT_VERSION = "b2a-object-layout-v1"

PROGRAM = "cgm-acquire"


def build_adapters() -> dict[str, SourceAdapter]:
    """Construct the adapter registry used by the CLI boundary."""
    return {
        "chess-results": ChessResultsAdapter(),
        "lichess-broadcast": LichessBroadcastAdapter(),
        "chesscom-broadcast": ChessComBroadcastAdapter(),
        "twic": TwicAdapter(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            "Acquire one B2 chess source reference and canonicalize it into "
            "the local registry. Prints one JSON object on stdout."
        ),
    )
    parser.add_argument(
        "source",
        metavar="SOURCE",
        help=(
            "Provider reference or supported URL: Chess-Results tnrNNNN, "
            "Lichess/Chess.com OTB broadcast feed or game, or TWIC issue 1660+. "
            "Adapters: chess-results, lichess-broadcast, chesscom-broadcast, twic."
        ),
    )
    parser.add_argument(
        "--registry",
        default=DEFAULT_REGISTRY_PATH,
        help=f"registry.sqlite path (default: {DEFAULT_REGISTRY_PATH}).",
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT_PATH,
        help=(
            "B2 local root; objects live in <root>/objects and transient "
            f"canonical/download working files in <root>/workspace "
            f"(default: {DEFAULT_ROOT_PATH})."
        ),
    )
    return parser


@dataclass(frozen=True)
class _Layout:
    registry: Path
    objects: Path
    workspace: Path


def _resolve_layout(registry_argument: str, root_argument: str) -> _Layout:
    registry = Path(registry_argument).expanduser().resolve()
    root = Path(root_argument).expanduser().resolve()
    if registry.is_dir():
        raise ValueError("registry path must be a file, not a directory")
    if root.is_file():
        raise ValueError("root path must be a directory, not a file")
    registry.parent.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)
    return _Layout(
        registry=registry,
        objects=root / "objects",
        workspace=root / "workspace",
    )


def _result_payload(
    result: AcquisitionResult,
    layout: _Layout,
) -> dict[str, object]:
    return {
        "ok": True,
        "layout_version": _LAYOUT_VERSION,
        "layout": {
            "registry": str(layout.registry),
            "objects": str(layout.objects),
            "workspace": str(layout.workspace),
        },
        "source": {
            "provider": result.source_ref.provider,
            "external_id": result.source_ref.external_id,
            "source_url": result.source_ref.source_url,
            "source_id": result.source_id,
        },
        "tournament": {
            "id": result.tournament_id,
            "status": result.tournament_status,
        },
        "raw": {
            "source_file_id": result.source_file_id,
            "object_key": result.raw_object_key,
            "sha256": result.raw_sha256,
            "byte_size": result.raw_byte_size,
            "download_attempt_id": result.download_attempt_id,
        },
        "canonical": {
            "revision_id": result.revision_id,
            "revision_number": result.revision_number,
            "sha256": result.canonical_sha256,
            "game_count": result.canonical_game_count,
            "ply_count": result.canonical_ply_count,
            "path": str(result.canonical_path),
        },
        "acquisition": {
            "source_tournament_id": result.source_tournament_id,
            "download_attempt_id": result.download_attempt_id,
            "source_game_count": result.source_game_count,
            "valid_game_count": result.valid_game_count,
            "invalid_game_count": result.invalid_game_count,
        },
    }


def _error_payload(error: BaseException) -> dict[str, object]:
    message = " ".join(str(error).split())
    return {
        "ok": False,
        "error": {
            "type": type(error).__name__,
            "message": message or type(error).__name__,
        },
    }


def _emit(payload: dict[str, object], *, stream) -> None:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
    stream.write("\n")
    stream.flush()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(arguments)

    try:
        layout = _resolve_layout(args.registry, args.root)
        registry = Registry(layout.registry)
        store = LocalObjectStore(layout.objects)
        service = AcquisitionService(
            registry=registry,
            store=store,
            workspace=layout.workspace,
            adapters=build_adapters(),
        )
        result = service.acquire(args.source)
        payload = _result_payload(result, layout)
    except KeyboardInterrupt:
        _emit(_error_payload(KeyboardInterrupt("acquisition interrupted")), stream=sys.stderr)
        return 130
    except Exception as exc:
        _emit(_error_payload(exc), stream=sys.stdout)
        return 1

    _emit(payload, stream=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
