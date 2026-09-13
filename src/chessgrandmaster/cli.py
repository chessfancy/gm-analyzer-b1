import argparse
from pathlib import Path

from .platform_policy import (
    PLATFORM_PROFILES,
    resolve_platform_policy,
)
from .production_pipeline import run_pipeline


def positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "must be a positive integer"
        ) from exc

    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            "must be a positive integer"
        )

    return parsed


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cgm-analyze",
        description=(
            "Analyze a PGN with Stockfish using "
            "Lucas-compatible ChessGrandmaster semantics."
        ),
    )

    parser.add_argument(
        "--platform-profile",
        choices=sorted(PLATFORM_PROFILES),
        default=None,
        help="Named resource policy profile",
    )

    parser.add_argument(
        "--workers",
        type=positive_int,
        default=None,
        help="Parallel Stockfish workers",
    )

    parser.add_argument(
        "--threads",
        type=positive_int,
        default=None,
        help="Stockfish threads per worker",
    )

    parser.add_argument(
        "--hash-mb",
        type=positive_int,
        default=None,
        help="Stockfish Hash in MB per worker",
    )

    parser.add_argument(
        "--depth",
        type=positive_int,
        default=18,
        help="Stockfish final search depth (default: 18)",
    )

    parser.add_argument(
        "pgn",
        type=Path,
        help="Input PGN file",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    policy = resolve_platform_policy(
        args.platform_profile,
        workers=args.workers,
        threads=args.threads,
        hash_mb=args.hash_mb,
    )

    run_pipeline(
        args.pgn,
        workers=policy["workers"],
        threads=policy["threads"],
        hash_mb=policy["hash_mb"],
        depth=args.depth,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
