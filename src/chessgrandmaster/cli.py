import argparse
from pathlib import Path

from .production_pipeline import run_pipeline


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
        type=int,
        default=2,
        help="Parallel Stockfish workers (default: 2)",
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
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
