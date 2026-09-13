import argparse
from pathlib import Path

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
        "--workers",
        type=positive_int,
        default=2,
        help="Parallel Stockfish workers (default: 2)",
    )

    parser.add_argument(
        "--hash-mb",
        type=positive_int,
        default=256,
        help="Stockfish Hash in MB per worker (default: 256)",
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

    run_pipeline(
        args.pgn,
        workers=args.workers,
        hash_mb=args.hash_mb,
        depth=args.depth,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
