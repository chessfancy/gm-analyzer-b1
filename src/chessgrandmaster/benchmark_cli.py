import sys


TOP_LEVEL_HELP = """usage: cgm-bench {qualify,compare} ...

Benchmark and qualify compute platforms for ChessGrandmaster.

commands:
  qualify   Measure fixed work, time-to-depth, and full Lucas pipeline performance.
  compare   Compare compatible qualification JSON reports.

Run `cgm-bench qualify --help` or `cgm-bench compare --help` for details.
"""


def run_compare(argv):
    from .benchmark_compare import main

    return main(argv)


def run_qualify(argv):
    from .benchmark import main

    return main(argv)


def main(argv=None):
    args = list(
        sys.argv[1:]
        if argv is None
        else argv
    )

    if args in (["-h"], ["--help"]):
        print(
            TOP_LEVEL_HELP,
            end="",
        )
        return 0

    if args and args[0] == "compare":
        return run_compare(
            args[1:]
        )

    return run_qualify(
        args
    )


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
