"""``cgm-fetch``: fetch a corpus's immutable raw PGNs without processing games."""

from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys
from typing import Sequence

from ..discovery.acquire import DiscoveryAcquisitionResult
from ..discovery.chess_results import (
    ChessResultsDiscovery,
    CorpusDiscoveryResult,
    PROVIDER,
)
from ..discovery.cli import _build_acquisition_service as _build_discovery_service


PROGRAM = "cgm-fetch"


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog=PROGRAM,
        description=(
            "Fetch immutable Chess-Results corpus PGNs without validating or "
            "canonicalizing games. Prints one JSON object on stdout."
        ),
    )
    providers = parser.add_subparsers(
        dest="provider",
        required=True,
        parser_class=_JsonArgumentParser,
    )
    chess_results = providers.add_parser(
        PROVIDER,
        help="fetch Chess-Results raw PGNs",
    )
    modes = chess_results.add_subparsers(
        dest="mode",
        required=True,
        parser_class=_JsonArgumentParser,
    )
    corpus = modes.add_parser(
        "corpus",
        help="scan and fetch the full-year standard/classical corpus",
    )
    corpus.add_argument("--year", type=int, default=2026)
    corpus.add_argument("--max-lines", type=_positive_int, default=2000)
    corpus.add_argument(
        "--limit",
        type=_positive_int,
        help="limit fetching only after the complete scan and priority enrichment",
    )
    corpus.add_argument("--refresh-recent-days", type=_non_negative_int, default=0)
    corpus.add_argument("--timeout", type=float, default=30.0)
    corpus.add_argument("--registry", required=True)
    corpus.add_argument("--root", required=True)
    corpus.add_argument("--report")
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _error_payload(error: BaseException) -> dict[str, object]:
    message = " ".join(str(error).split())
    return {
        "ok": False,
        "error": {
            "type": type(error).__name__,
            "message": message or type(error).__name__,
        },
    }


def _build_fetch_service(
    registry_argument: str,
    root_argument: str,
):
    """Reuse the existing B2 registry/store/provider bridge construction."""
    return _build_discovery_service(registry_argument, root_argument)


def _priority_counts(result: CorpusDiscoveryResult) -> dict[str, int]:
    return {
        "book_high": result.priority_counts["BOOK_HIGH"],
        "book_medium": result.priority_counts["BOOK_MEDIUM"],
        "general": result.priority_counts["GENERAL"],
    }


def _result_payload(
    result: CorpusDiscoveryResult,
    acquisition: DiscoveryAcquisitionResult | None = None,
) -> dict[str, object]:
    batch = acquisition
    return {
        "ok": batch is None or batch.failed == 0,
        "provider": PROVIDER,
        "mode": "corpus",
        "candidate_count": len(result.candidates),
        "complete": result.complete,
        "priority_complete": result.priority_complete,
        "priority_errors": list(result.priority_errors),
        "priority_counts": _priority_counts(result),
        "fetched": 0 if batch is None else batch.fetched,
        "skipped_existing": 0 if batch is None else batch.skipped_existing,
        "skipped_no_pgn": 0 if batch is None else batch.skipped_no_pgn,
        "failed": 0 if batch is None else batch.failed,
        "results": [] if batch is None else [item.to_dict() for item in batch.results],
        "errors": list(result.errors),
        "saturated_windows": [
            window.to_dict() for window in result.saturated_windows
        ],
        "windows": [window.to_dict() for window in result.windows],
    }


def _incomplete_payload(result: CorpusDiscoveryResult) -> dict[str, object]:
    payload = _result_payload(result)
    payload["ok"] = False
    payload["error"] = {
        "type": "CorpusIncompleteError",
        "message": "corpus discovery is incomplete",
    }
    return payload


def _write_report(
    path: str,
    result: CorpusDiscoveryResult,
    *,
    year: int,
    max_lines: int,
    refresh_recent_days: int,
    acquisition: DiscoveryAcquisitionResult | None,
) -> None:
    report_path = Path(path).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _result_payload(result, acquisition)
    payload.update(
        {
            "year": year,
            "max_lines": max_lines,
            "refresh_recent_days": refresh_recent_days,
            "candidates": [candidate.to_dict() for candidate in result.candidates],
        }
    )
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
        _emit(_error_payload(exc))
        return 2

    if args.provider != PROVIDER or args.mode != "corpus":
        _emit(_error_payload(ValueError("unsupported fetch mode")))
        return 2
    if args.timeout <= 0:
        _emit(_error_payload(ValueError("timeout must be positive")))
        return 2

    result: CorpusDiscoveryResult
    try:
        discovery = ChessResultsDiscovery(timeout_sec=args.timeout)
        scanned = discovery.discover_corpus(
            year=args.year,
            max_lines=args.max_lines,
            limit=None,
            country=None,
        )
        if not scanned.complete:
            result = scanned
            if args.report:
                _write_report(
                    args.report,
                    result,
                    year=args.year,
                    max_lines=args.max_lines,
                    refresh_recent_days=args.refresh_recent_days,
                    acquisition=None,
                )
            _emit(_incomplete_payload(result))
            return 2

        result = discovery.enrich_corpus_priority(
            scanned,
            year=args.year,
            limit=None,
        )
        if args.limit is not None:
            result = CorpusDiscoveryResult(
                candidates=result.candidates[: args.limit],
                windows=result.windows,
                errors=result.errors,
                priority_errors=result.priority_errors,
            )
    except Exception as exc:
        _emit(_error_payload(exc))
        return 2

    try:
        service = _build_fetch_service(args.registry, args.root)
        acquisition = service.fetch_candidates(
            result.candidates,
            refresh_recent_days=args.refresh_recent_days,
        )
        if args.report:
            _write_report(
                args.report,
                result,
                year=args.year,
                max_lines=args.max_lines,
                refresh_recent_days=args.refresh_recent_days,
                acquisition=acquisition,
            )
    except Exception as exc:
        _emit(_error_payload(exc))
        return 2

    _emit(_result_payload(result, acquisition))
    return 1 if acquisition.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
