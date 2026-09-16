"""Command-line entry point for read-only candidate discovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from ..b2.acquisition import AcquisitionService
from ..b2.cli_acquire import (
    DEFAULT_REGISTRY_PATH,
    DEFAULT_ROOT_PATH,
    build_adapters,
)
from ..b2.registry import Registry
from ..b2.storage import LocalObjectStore
from .acquire import DiscoveryAcquisitionResult, DiscoveryAcquisitionService
from .chess_results import (
    ChessResultsDiscovery,
    CorpusDiscoveryResult,
    DiscoveryResult,
    PROVIDER,
)


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


def _add_http_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--limit", type=_positive_int)


def _add_acquisition_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--acquire",
        action="store_true",
        help="acquire discovered candidates through the B2a service",
    )
    parser.add_argument(
        "--registry",
        default=DEFAULT_REGISTRY_PATH,
        help=f"registry.sqlite path (default: {DEFAULT_REGISTRY_PATH}).",
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT_PATH,
        help=f"B2 local root (default: {DEFAULT_ROOT_PATH}).",
    )


def _add_date_options(
    parser: argparse.ArgumentParser, *, required: bool = True
) -> None:
    parser.add_argument("--from", dest="from_date", required=required)
    parser.add_argument("--to", dest="to_date", required=required)


def _add_corpus_options(
    parser: argparse.ArgumentParser,
    *,
    allow_country: bool,
    allow_refresh: bool = False,
) -> None:
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--max-lines", type=_positive_int, default=2000)
    parser.add_argument("--limit", type=_positive_int)
    if allow_country:
        parser.add_argument("--country")
    if allow_refresh:
        parser.add_argument(
            "--refresh-recent-days",
            type=_non_negative_int,
            default=60,
            help="refresh completed recent tournaments from this many days (default: 60)",
        )
    parser.add_argument("--report")
    _add_http_options_without_limit(parser)
    _add_acquisition_options(parser)


def _add_http_options_without_limit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=30.0)


def _build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="cgm-discover",
        description="Read-only Chess-Results candidate discovery",
    )
    providers = parser.add_subparsers(
        dest="provider", required=True, parser_class=_JsonArgumentParser
    )
    chess_results = providers.add_parser(
        PROVIDER,
        help="discover candidates from Chess-Results",
    )
    modes = chess_results.add_subparsers(
        dest="mode", required=True, parser_class=_JsonArgumentParser
    )

    federation = modes.add_parser(
        "federation", help="list tournaments from a federation feed"
    )
    federation.add_argument("federation")
    _add_http_options(federation)
    _add_acquisition_options(federation)

    overseas = modes.add_parser(
        "overseas-vie", help="search VIE players in overseas tournaments"
    )
    _add_date_options(overseas)
    _add_http_options(overseas)
    _add_acquisition_options(overseas)

    diaspora = modes.add_parser(
        "diaspora", help="search bounded surname seeds with strong name hints"
    )
    _add_date_options(diaspora)
    _add_http_options(diaspora)
    _add_acquisition_options(diaspora)

    player = modes.add_parser(
        "player", help="search one FIDE ID"
    )
    player.add_argument("fide_id")
    _add_date_options(player, required=False)
    _add_http_options(player)
    _add_acquisition_options(player)

    family = modes.add_parser(
        "family", help="follow explicit family links from one tournament"
    )
    family.add_argument("seed")
    _add_http_options(family)
    _add_acquisition_options(family)

    corpus = modes.add_parser(
        "corpus",
        help="scan the full-year standard/classical downloadable corpus",
    )
    _add_corpus_options(corpus, allow_country=False, allow_refresh=True)

    downloadable = modes.add_parser(
        "downloadable",
        help="diagnostic downloadable Tournament Database scan",
    )
    _add_corpus_options(downloadable, allow_country=True, allow_refresh=False)
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _error_payload(
    provider: str | None,
    mode: str | None,
    exc: Exception,
) -> dict[str, object]:
    return {
        "ok": False,
        "provider": provider,
        "mode": mode,
        "error": {"type": type(exc).__name__, "message": str(exc)},
    }


def _success_payload(
    provider: str,
    mode: str,
    result: DiscoveryResult | CorpusDiscoveryResult,
    acquisition: DiscoveryAcquisitionResult | None = None,
) -> dict[str, object]:
    candidates = [candidate.to_dict() for candidate in result.candidates]
    payload: dict[str, object] = {
        "ok": True,
        "provider": provider,
        "mode": mode,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "errors": list(result.errors),
    }
    if isinstance(result, CorpusDiscoveryResult):
        payload["complete"] = result.complete
        payload["priority_complete"] = result.priority_complete
        payload["priority_errors"] = list(result.priority_errors)
        payload["windows"] = [window.to_dict() for window in result.windows]
        payload["priority_counts"] = {
            "book_high": result.priority_counts["BOOK_HIGH"],
            "book_medium": result.priority_counts["BOOK_MEDIUM"],
            "general": result.priority_counts["GENERAL"],
        }
    if acquisition is not None:
        payload["ok"] = acquisition.failed == 0
        payload["acquisition"] = acquisition.to_dict()
    return payload


def _corpus_incomplete_payload(
    provider: str,
    mode: str,
    result: CorpusDiscoveryResult,
) -> dict[str, object]:
    return {
        "ok": False,
        "provider": provider,
        "mode": mode,
        "complete": result.complete,
        "priority_complete": result.priority_complete,
        "priority_errors": list(result.priority_errors),
        "error": {
            "type": "CorpusIncompleteError",
            "message": "corpus discovery is incomplete",
        },
        "discovery": {
            "candidate_count": len(result.candidates),
            "errors": list(result.errors),
            "saturated_windows": [
                window.to_dict() for window in result.saturated_windows
            ],
        },
    }


def _corpus_report(
    result: CorpusDiscoveryResult,
    *,
    year: int,
    max_lines: int,
    country: str | None,
    acquisition: DiscoveryAcquisitionResult | None,
    refresh_recent_days: int = 0,
) -> dict[str, object]:
    action_by_identity = {}
    if acquisition is not None:
        action_by_identity = {
            (item.provider, item.external_id): item.to_dict()
            for item in acquisition.results
        }
    candidate_rows = []
    for candidate in result.candidates:
        row = candidate.to_dict()
        outcome = action_by_identity.get((candidate.provider, candidate.external_id), {})
        row.update(
            {
                "end_date": candidate.to_date,
                "time_control": candidate.time_control_hint,
                "acquisition_action": outcome.get("action"),
                "tournament_id": outcome.get("tournament_id"),
                "revision_id": outcome.get("revision_id"),
                "previous_raw_sha256": outcome.get("previous_raw_sha256"),
                "raw_sha256": outcome.get("raw_sha256"),
            }
        )
        candidate_rows.append(row)
    if acquisition is None:
        acquisition_payload = {
            "acquired": 0,
            "skipped_existing": 0,
            "skipped_no_pgn": 0,
            "refreshed_unchanged": 0,
            "refreshed_changed": 0,
            "refresh_unavailable": 0,
            "failed": 0,
        }
    else:
        acquisition_payload = {
            "acquired": acquisition.acquired,
            "skipped_existing": acquisition.skipped_existing,
            "skipped_no_pgn": acquisition.skipped_no_pgn,
            "refreshed_unchanged": acquisition.refreshed_unchanged,
            "refreshed_changed": acquisition.refreshed_changed,
            "refresh_unavailable": acquisition.refresh_unavailable,
            "failed": acquisition.failed,
        }
    return {
        "year": year,
        "time_control": "standard/classical",
        "only_finished": True,
        "games_available": True,
        "max_lines": max_lines,
        "country": country,
        "refresh_recent_days": refresh_recent_days,
        "complete": result.complete,
        "priority_complete": result.priority_complete,
        "priority_errors": list(result.priority_errors),
        "windows": [window.to_dict() for window in result.windows],
        "saturated_windows": [
            window.to_dict() for window in result.saturated_windows
        ],
        "candidate_count": len(result.candidates),
        "priority_counts": {
            "book_high": result.priority_counts["BOOK_HIGH"],
            "book_medium": result.priority_counts["BOOK_MEDIUM"],
            "general": result.priority_counts["GENERAL"],
        },
        "acquisition": acquisition_payload,
        "errors": list(result.errors),
        "candidates": candidate_rows,
    }


def _write_corpus_report(
    path: str,
    result: CorpusDiscoveryResult,
    *,
    year: int,
    max_lines: int,
    country: str | None,
    acquisition: DiscoveryAcquisitionResult | None,
    refresh_recent_days: int = 0,
) -> None:
    report_path = Path(path).expanduser()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            _corpus_report(
                result,
                year=year,
                max_lines=max_lines,
                country=country,
                acquisition=acquisition,
                refresh_recent_days=refresh_recent_days,
            ),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _build_acquisition_service(
    registry_argument: str,
    root_argument: str,
) -> DiscoveryAcquisitionService:
    registry_path = Path(registry_argument).expanduser().resolve()
    root_path = Path(root_argument).expanduser().resolve()
    if registry_path.is_dir():
        raise ValueError("registry path must be a file, not a directory")
    if root_path.is_file():
        raise ValueError("root path must be a directory, not a file")
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    root_path.mkdir(parents=True, exist_ok=True)

    registry = Registry(registry_path)
    store = LocalObjectStore(root_path / "objects")
    acquisition = AcquisitionService(
        registry=registry,
        store=store,
        workspace=root_path / "workspace",
        adapters=build_adapters(),
    )

    def pgn_probe(candidate):
        adapter = acquisition.adapters.get(candidate.provider)
        if adapter is None:
            raise ValueError(
                f"no acquisition adapter for provider {candidate.provider!r}"
            )
        refs = adapter.discover(candidate.source_url)
        if len(refs) != 1:
            raise RuntimeError(
                f"provider {candidate.provider!r} returned an unexpected "
                "source-reference count for candidate"
            )
        ref = refs[0]
        if (
            ref.provider != candidate.provider
            or ref.external_id.casefold() != candidate.external_id.casefold()
        ):
            raise RuntimeError("provider source reference does not match candidate")
        probe = getattr(adapter, "probe_pgn", None)
        if not callable(probe):
            raise RuntimeError(
                f"provider {candidate.provider!r} has no PGN availability probe"
            )
        return probe(ref)

    return DiscoveryAcquisitionService(registry, acquisition, pgn_probe)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    except Exception as exc:
        _emit(_error_payload(None, None, exc))
        return 2

    try:
        service = ChessResultsDiscovery(timeout_sec=args.timeout)
        if args.provider != PROVIDER:
            raise ValueError(f"unsupported provider: {args.provider}")
        if args.mode in {"corpus", "downloadable"}:
            scanned = service.discover_corpus(
                year=args.year,
                max_lines=args.max_lines,
                limit=None,
                country=getattr(args, "country", None),
            )
            if args.mode == "corpus":
                if args.acquire and not scanned.complete:
                    result = scanned
                else:
                    result = service.enrich_corpus_priority(
                        scanned,
                        year=args.year,
                        limit=args.limit,
                    )
            else:
                candidates = scanned.candidates
                if args.limit is not None:
                    candidates = candidates[: args.limit]
                result = CorpusDiscoveryResult(
                    candidates=tuple(candidates),
                    windows=scanned.windows,
                    errors=scanned.errors,
                    priority_errors=scanned.priority_errors,
                )
        elif args.mode == "federation":
            result = service.discover_federation(args.federation, limit=args.limit)
        elif args.mode == "overseas-vie":
            result = service.discover_overseas_vie(
                from_date=args.from_date,
                to_date=args.to_date,
                limit=args.limit,
            )
        elif args.mode == "diaspora":
            result = service.discover_diaspora(
                from_date=args.from_date,
                to_date=args.to_date,
                limit=args.limit,
            )
        elif args.mode == "player":
            result = service.discover_player(
                args.fide_id,
                from_date=args.from_date,
                to_date=args.to_date,
                limit=args.limit,
            )
        elif args.mode == "family":
            result = service.expand_family(args.seed, limit=args.limit)
        else:
            raise ValueError(f"unsupported mode: {args.mode}")
    except Exception as exc:
        _emit(_error_payload(args.provider, args.mode, exc))
        return 2

    if not args.acquire:
        if getattr(args, "report", None):
            try:
                if not isinstance(result, CorpusDiscoveryResult):
                    raise ValueError("--report is only valid for corpus modes")
                _write_corpus_report(
                    args.report,
                    result,
                    year=args.year,
                    max_lines=args.max_lines,
                    country=getattr(args, "country", None),
                    acquisition=None,
                    refresh_recent_days=getattr(args, "refresh_recent_days", 0),
                )
            except Exception as exc:
                _emit(_error_payload(args.provider, args.mode, exc))
                return 2
        _emit(_success_payload(args.provider, args.mode, result))
        return 0

    if args.mode == "corpus" and not result.complete:
        if getattr(args, "report", None):
            try:
                _write_corpus_report(
                    args.report,
                    result,
                    year=args.year,
                    max_lines=args.max_lines,
                    country=getattr(args, "country", None),
                    acquisition=None,
                    refresh_recent_days=getattr(args, "refresh_recent_days", 0),
                )
            except Exception as exc:
                _emit(_error_payload(args.provider, args.mode, exc))
                return 2
        _emit(_corpus_incomplete_payload(args.provider, args.mode, result))
        return 2

    try:
        acquisition_service = _build_acquisition_service(
            args.registry,
            args.root,
        )
        if args.mode == "corpus":
            acquisition = acquisition_service.acquire_candidates(
                result.candidates,
                refresh_recent_days=args.refresh_recent_days,
            )
        else:
            acquisition = acquisition_service.acquire_candidates(result.candidates)
    except Exception as exc:
        _emit(_error_payload(args.provider, args.mode, exc))
        return 2

    if getattr(args, "report", None):
        try:
            if not isinstance(result, CorpusDiscoveryResult):
                raise ValueError("--report is only valid for corpus modes")
            _write_corpus_report(
                args.report,
                result,
                year=args.year,
                max_lines=args.max_lines,
                country=getattr(args, "country", None),
                acquisition=acquisition,
                refresh_recent_days=getattr(args, "refresh_recent_days", 0),
            )
        except Exception as exc:
            _emit(_error_payload(args.provider, args.mode, exc))
            return 2

    _emit(_success_payload(args.provider, args.mode, result, acquisition))
    return 1 if acquisition.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
