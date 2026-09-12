import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import socket
import statistics
import time

import chess
import chess.engine
import chess.pgn

from .engine_manifest import (
    resolve_installed_engine,
    verify_engine_binary,
)
from .engine_worker import LucasEngineWorker


def percentile(values, percent):
    values = sorted(
        float(value)
        for value in values
    )

    if not values:
        raise ValueError(
            "percentile requires at least one value"
        )

    if not 0 <= percent <= 100:
        raise ValueError(
            "percent must be between 0 and 100"
        )

    if len(values) == 1:
        return values[0]

    rank = (
        (len(values) - 1)
        * percent
        / 100.0
    )

    lower = math.floor(rank)
    upper = math.ceil(rank)

    if lower == upper:
        return values[lower]

    fraction = rank - lower

    return (
        values[lower]
        + (
            values[upper]
            - values[lower]
        )
        * fraction
    )


def parse_depths(value):
    try:
        depths = [
            int(part.strip())
            for part in value.split(",")
            if part.strip()
        ]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "depths must be comma-separated integers"
        ) from exc

    if not depths:
        raise argparse.ArgumentTypeError(
            "at least one depth is required"
        )

    if any(
        depth <= 0
        for depth in depths
    ):
        raise argparse.ArgumentTypeError(
            "depths must be positive"
        )

    return depths


def recommend_depth(
    rows,
    target_p95_seconds,
):
    qualifying = [
        int(row["depth"])
        for row in rows
        if (
            float(row["p95_seconds"])
            <= target_p95_seconds
        )
    ]

    if not qualifying:
        return None

    return max(
        qualifying
    )


def _read_text(path):
    try:
        return Path(path).read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return None


def detect_effective_cpu():
    logical = (
        os.cpu_count()
        or 1
    )

    quota_cpu = None

    cpu_max = _read_text(
        "/sys/fs/cgroup/cpu.max"
    )

    if cpu_max:
        parts = cpu_max.split()

        if (
            len(parts) >= 2
            and parts[0] != "max"
        ):
            try:
                quota = float(
                    parts[0]
                )
                period = float(
                    parts[1]
                )

                if period > 0:
                    quota_cpu = (
                        quota
                        / period
                    )
            except ValueError:
                quota_cpu = None

    effective = float(
        logical
    )

    if quota_cpu is not None:
        effective = min(
            effective,
            quota_cpu,
        )

    effective = max(
        1.0,
        effective,
    )

    return {
        "logical_cpu": logical,
        "cgroup_cpu_quota": quota_cpu,
        "effective_cpu": effective,
        "suggested_workers": max(
            1,
            int(
                math.floor(
                    effective
                )
            ),
        ),
    }


def detect_memory_bytes():
    memory_max = _read_text(
        "/sys/fs/cgroup/memory.max"
    )

    if (
        memory_max
        and memory_max != "max"
    ):
        try:
            value = int(
                memory_max
            )

            if value > 0:
                return value
        except ValueError:
            pass

    meminfo = _read_text(
        "/proc/meminfo"
    )

    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith(
                "MemTotal:"
            ):
                parts = line.split()

                if len(parts) >= 2:
                    return (
                        int(parts[1])
                        * 1024
                    )

    return None


def hardware_info():
    cpu = detect_effective_cpu()

    memory = detect_memory_bytes()

    return {
        "hostname":
            socket.gethostname(),

        "platform":
            platform.platform(),

        "machine":
            platform.machine(),

        "processor":
            platform.processor(),

        **cpu,

        "memory_bytes":
            memory,

        "memory_gib":
            (
                memory
                / (1024 ** 3)
                if memory
                else None
            ),
    }


def load_pgn_positions(
    pgn_path,
):
    pgn_path = Path(
        pgn_path
    )

    records = []

    with pgn_path.open(
        encoding="utf-8",
        errors="replace",
    ) as handle:

        game_index = 0

        while True:
            game = chess.pgn.read_game(
                handle
            )

            if game is None:
                break

            game_index += 1

            board = game.board()

            for ply, move in enumerate(
                game.mainline_moves(),
                start=1,
            ):
                records.append({
                    "game_index":
                        game_index,

                    "ply":
                        ply,

                    "fen":
                        board.fen(),

                    "played_uci":
                        move.uci(),

                    "san":
                        board.san(
                            move
                        ),
                })

                board.push(
                    move
                )

    if not records:
        raise RuntimeError(
            f"No moves found in {pgn_path}"
        )

    return records


def sample_positions(
    records,
    count=12,
    skip_opening=8,
):
    eligible = [
        record
        for record in records
        if (
            record["ply"]
            > skip_opening
        )
    ]

    if not eligible:
        eligible = list(
            records
        )

    if count <= 0:
        raise ValueError(
            "sample count must be positive"
        )

    if len(eligible) <= count:
        return list(
            eligible
        )

    if count == 1:
        indices = [
            len(eligible) // 2
        ]
    else:
        indices = [
            round(
                i
                * (
                    len(eligible)
                    - 1
                )
                / (
                    count
                    - 1
                )
            )
            for i in range(
                count
            )
        ]

    selected = [
        eligible[index]
        for index in indices
    ]

    # Keep the known tactical position from the
    # existing golden game when available.
    tactical = next(
        (
            record
            for record in eligible
            if (
                record["game_index"]
                == 1
                and record["ply"]
                == 42
            )
        ),
        None,
    )

    if (
        tactical is not None
        and tactical not in selected
    ):
        selected[
            len(selected) // 2
        ] = tactical

    unique = {}

    for record in selected:
        key = (
            record["game_index"],
            record["ply"],
        )

        unique[key] = record

    selected = list(
        unique.values()
    )

    # Rounding can theoretically deduplicate an index.
    # Refill from eligible positions if needed.
    if len(selected) < count:
        existing = {
            (
                row["game_index"],
                row["ply"],
            )
            for row in selected
        }

        for record in eligible:
            key = (
                record["game_index"],
                record["ply"],
            )

            if key in existing:
                continue

            selected.append(
                record
            )

            existing.add(
                key
            )

            if len(selected) >= count:
                break

    selected.sort(
        key=lambda row: (
            row["game_index"],
            row["ply"],
        )
    )

    return selected[
        :count
    ]


def _clear_hash(
    engine,
):
    if (
        "Clear Hash"
        in engine.options
    ):
        engine.configure({
            "Clear Hash": None,
        })


def _normalize_info(
    info,
):
    if isinstance(
        info,
        list,
    ):
        if not info:
            raise RuntimeError(
                "Stockfish returned no analysis"
            )

        return info[0]

    return info


def _summarize_raw(
    rows,
):
    elapsed = [
        row["elapsed_seconds"]
        for row in rows
    ]

    nodes = [
        row["nodes"]
        for row in rows
        if row["nodes"] > 0
    ]

    nps = [
        row["nps"]
        for row in rows
        if row["nps"] > 0
    ]

    depths = [
        row["depth"]
        for row in rows
        if row["depth"] > 0
    ]

    return {
        "samples":
            len(rows),

        "median_seconds":
            statistics.median(
                elapsed
            ),

        "p95_seconds":
            percentile(
                elapsed,
                95,
            ),

        "max_seconds":
            max(
                elapsed
            ),

        "mean_seconds":
            statistics.mean(
                elapsed
            ),

        "median_nodes":
            (
                statistics.median(
                    nodes
                )
                if nodes
                else 0
            ),

        "median_nps":
            (
                statistics.median(
                    nps
                )
                if nps
                else 0
            ),

        "median_depth":
            (
                statistics.median(
                    depths
                )
                if depths
                else 0
            ),
    }


def run_raw_benchmark(
    engine_path,
    positions,
    *,
    limit,
    hash_mb=256,
):
    engine = (
        chess.engine.SimpleEngine
        .popen_uci(
            str(engine_path)
        )
    )

    rows = []

    try:
        engine.configure({
            "Threads": 1,
            "Hash":
                int(hash_mb),
        })

        for record in positions:
            _clear_hash(
                engine
            )

            board = chess.Board(
                record["fen"]
            )

            started = (
                time.perf_counter()
            )

            info = engine.analyse(
                board,
                limit,
                multipv=1,
                info=(
                    chess.engine
                    .INFO_ALL
                ),
            )

            elapsed = (
                time.perf_counter()
                - started
            )

            info = _normalize_info(
                info
            )

            rows.append({
                "game_index":
                    record[
                        "game_index"
                    ],

                "ply":
                    record["ply"],

                "elapsed_seconds":
                    elapsed,

                "nodes":
                    int(
                        info.get(
                            "nodes",
                            0,
                        )
                    ),

                "nps":
                    int(
                        info.get(
                            "nps",
                            0,
                        )
                    ),

                "depth":
                    int(
                        info.get(
                            "depth",
                            0,
                        )
                    ),
            })

    finally:
        engine.quit()

    return {
        "summary":
            _summarize_raw(
                rows
            ),

        "rows":
            rows,
    }


def run_fixed_work_phase(
    engine_path,
    positions,
    *,
    nodes,
    hash_mb=256,
):
    result = run_raw_benchmark(
        engine_path,
        positions,
        limit=chess.engine.Limit(
            nodes=int(nodes)
        ),
        hash_mb=hash_mb,
    )

    result[
        "target_nodes"
    ] = int(
        nodes
    )

    return result


def run_depth_phase(
    engine_path,
    positions,
    *,
    depth,
    hash_mb=256,
):
    result = run_raw_benchmark(
        engine_path,
        positions,
        limit=chess.engine.Limit(
            depth=int(depth)
        ),
        hash_mb=hash_mb,
    )

    result[
        "depth"
    ] = int(
        depth
    )

    return result


def run_pipeline_phase(
    engine_path,
    positions,
    *,
    depth=18,
    hash_mb=256,
):
    rows = []

    with LucasEngineWorker(
        engine_path,
        threads=1,
        hash_mb=hash_mb,
        multipv=1,
        depth=depth,
        time_sec=0,
        nodes=0,
    ) as worker:

        for record in positions:
            started = (
                time.perf_counter()
            )

            result = worker.analyze_move(
                record["fen"],
                record[
                    "played_uci"
                ],
            )

            elapsed = (
                time.perf_counter()
                - started
            )

            rows.append({
                "game_index":
                    record[
                        "game_index"
                    ],

                "ply":
                    record["ply"],

                "elapsed_seconds":
                    elapsed,

                "second_search":
                    bool(
                        result.second_search
                    ),

                "category":
                    result.category,

                "response_nodes":
                    sum(
                        int(
                            response.nodes
                            or 0
                        )
                        for response
                        in result.responses
                    ),
            })

    elapsed = [
        row["elapsed_seconds"]
        for row in rows
    ]

    second_searches = sum(
        1
        for row in rows
        if row[
            "second_search"
        ]
    )

    return {
        "depth":
            int(depth),

        "samples":
            len(rows),

        "mean_seconds":
            statistics.mean(
                elapsed
            ),

        "median_seconds":
            statistics.median(
                elapsed
            ),

        "p95_seconds":
            percentile(
                elapsed,
                95,
            ),

        "max_seconds":
            max(
                elapsed
            ),

        "second_searches":
            second_searches,

        "second_search_rate":
            (
                second_searches
                / len(rows)
            ),

        "rows":
            rows,
    }


def build_report(
    *,
    pgn_path,
    engine_path,
    samples,
    fixed_nodes,
    depths,
    hash_mb,
    target_p95_seconds,
    tournament_moves,
):
    hardware = hardware_info()

    engine_info = (
        verify_engine_binary(
            engine_path
        )
    )

    records = load_pgn_positions(
        pgn_path
    )

    positions = sample_positions(
        records,
        count=samples,
    )

    # --------------------------------------------------------
    # Phase 1:
    # Fixed-work benchmark.
    #
    # This measures hardware throughput only.
    # It is NOT a production search policy.
    # --------------------------------------------------------

    fixed = (
        run_fixed_work_phase(
            engine_path,
            positions,
            nodes=fixed_nodes,
            hash_mb=hash_mb,
        )
    )

    # --------------------------------------------------------
    # Phase 2:
    # Raw Stockfish time-to-depth.
    #
    # Clear Hash between sampled positions so machines can be
    # compared using approximately the same isolated workload.
    # --------------------------------------------------------

    depth_rows = []

    for depth in depths:
        result = run_depth_phase(
            engine_path,
            positions,
            depth=depth,
            hash_mb=hash_mb,
        )

        depth_rows.append({
            "depth":
                depth,

            **result["summary"],
        })

    # --------------------------------------------------------
    # Phase 3:
    # FULL Lucas-compatible move analysis at EVERY depth.
    #
    # This includes:
    #   primary search
    #   +
    #   post-move second search when required.
    #
    # Production qualification MUST be based on this phase,
    # not on raw primary-search timing.
    # --------------------------------------------------------

    pipeline_rows = []

    for depth in depths:
        pipeline = (
            run_pipeline_phase(
                engine_path,
                positions,
                depth=depth,
                hash_mb=hash_mb,
            )
        )

        pipeline_rows.append({
            key: value
            for key, value
            in pipeline.items()
            if key != "rows"
        })

    # --------------------------------------------------------
    # Recommendation
    #
    # Highest tested depth whose FULL Lucas pipeline P95 fits
    # the operational target.
    # --------------------------------------------------------

    suggested_depth = (
        recommend_depth(
            pipeline_rows,
            target_p95_seconds,
        )
    )

    # --------------------------------------------------------
    # Capacity estimates per depth
    # --------------------------------------------------------

    workers = hardware[
        "suggested_workers"
    ]

    capacity_rows = []

    for pipeline in pipeline_rows:
        one_worker_minutes = (
            pipeline["mean_seconds"]
            * tournament_moves
            / 60.0
        )

        ideal_parallel_minutes = (
            one_worker_minutes
            / workers
        )

        capacity_rows.append({
            "depth":
                pipeline["depth"],

            "tournament_moves":
                tournament_moves,

            "mean_seconds_per_move":
                pipeline["mean_seconds"],

            "one_worker_minutes":
                one_worker_minutes,

            "suggested_workers":
                workers,

            "ideal_parallel_minutes":
                ideal_parallel_minutes,
        })

    return {
        "schema_version":
            2,

        "created_at":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "source_pgn":
            str(
                Path(
                    pgn_path
                ).resolve()
            ),

        "sample_count":
            len(
                positions
            ),

        "sample_positions": [
            {
                "game_index":
                    row[
                        "game_index"
                    ],

                "ply":
                    row["ply"],

                "san":
                    row["san"],
            }
            for row in positions
        ],

        "hardware":
            hardware,

        "engine": {
            "uci_name":
                engine_info[
                    "uci_name"
                ],

            "path":
                engine_info[
                    "path"
                ],

            "binary_sha256":
                engine_info[
                    "binary_sha256"
                ],

            "threads":
                1,

            "hash_mb":
                hash_mb,

            "multipv":
                1,
        },

        "fixed_work": {
            "nodes":
                fixed_nodes,

            **fixed["summary"],
        },

        "depth_benchmarks":
            depth_rows,

        "pipeline_benchmarks":
            pipeline_rows,

        "target_p95_seconds":
            target_p95_seconds,

        "suggested_depth":
            suggested_depth,

        "suggested_depth_basis":
            "full_lucas_pipeline_p95",

        "capacity_estimates":
            capacity_rows,

        "capacity_note": (
            "Parallel estimates are idealized. "
            "Real throughput must be validated "
            "with a full batch run."
        ),
    }


def _fmt_seconds(
    value,
):
    return (
        f"{value:.3f}s"
    )


def print_report(
    report,
):
    hardware = report[
        "hardware"
    ]

    engine = report[
        "engine"
    ]

    fixed = report[
        "fixed_work"
    ]

    print()
    print(
        "=" * 76
    )
    print(
        "CHESSGRANDMASTER COMPUTE QUALIFICATION"
    )
    print(
        "=" * 76
    )

    # --------------------------------------------------------
    # Engine
    # --------------------------------------------------------

    print()
    print(
        "ENGINE"
    )

    print(
        "  Name          :",
        engine["uci_name"],
    )

    print(
        "  Threads       :",
        engine["threads"],
    )

    print(
        "  Hash          :",
        f"{engine['hash_mb']} MB",
    )

    print(
        "  MultiPV       :",
        engine["multipv"],
    )

    # --------------------------------------------------------
    # Hardware
    # --------------------------------------------------------

    print()
    print(
        "HARDWARE"
    )

    print(
        "  Host          :",
        hardware["hostname"],
    )

    print(
        "  Logical CPUs  :",
        hardware[
            "logical_cpu"
        ],
    )

    print(
        "  CPU quota     :",
        hardware[
            "cgroup_cpu_quota"
        ],
    )

    print(
        "  Effective CPU :",
        round(
            hardware[
                "effective_cpu"
            ],
            2,
        ),
    )

    print(
        "  Worker guess  :",
        hardware[
            "suggested_workers"
        ],
    )

    memory_gib = hardware.get(
        "memory_gib"
    )

    print(
        "  Memory        :",
        (
            f"{memory_gib:.2f} GiB"
            if memory_gib
            is not None
            else "unknown"
        ),
    )

    # --------------------------------------------------------
    # Fixed work
    # --------------------------------------------------------

    print()
    print(
        "FIXED WORK"
    )

    print(
        "  Target nodes  :",
        f"{fixed['nodes']:,}",
    )

    print(
        "  Median time   :",
        _fmt_seconds(
            fixed[
                "median_seconds"
            ]
        ),
    )

    print(
        "  P95 time      :",
        _fmt_seconds(
            fixed[
                "p95_seconds"
            ]
        ),
    )

    print(
        "  Median NPS    :",
        f"{fixed['median_nps']:,.0f}",
    )

    # --------------------------------------------------------
    # Raw search
    # --------------------------------------------------------

    print()
    print(
        "RAW TIME TO DEPTH"
    )

    print(
        "  "
        + "Depth".ljust(8)
        + "Median".rjust(12)
        + "P95".rjust(12)
        + "Max".rjust(12)
        + "Nodes".rjust(16)
    )

    for row in report[
        "depth_benchmarks"
    ]:
        print(
            "  "
            + str(
                row["depth"]
            ).ljust(8)

            + _fmt_seconds(
                row[
                    "median_seconds"
                ]
            ).rjust(12)

            + _fmt_seconds(
                row[
                    "p95_seconds"
                ]
            ).rjust(12)

            + _fmt_seconds(
                row[
                    "max_seconds"
                ]
            ).rjust(12)

            + f"{row['median_nodes']:,.0f}".rjust(
                16
            )
        )

    # --------------------------------------------------------
    # Full Lucas pipeline
    # --------------------------------------------------------

    print()
    print(
        "FULL LUCAS PIPELINE"
    )

    print(
        "  "
        + "Depth".ljust(8)
        + "Median".rjust(12)
        + "P95".rjust(12)
        + "Max".rjust(12)
        + "2nd search".rjust(14)
    )

    for row in report[
        "pipeline_benchmarks"
    ]:
        print(
            "  "
            + str(
                row["depth"]
            ).ljust(8)

            + _fmt_seconds(
                row[
                    "median_seconds"
                ]
            ).rjust(12)

            + _fmt_seconds(
                row[
                    "p95_seconds"
                ]
            ).rjust(12)

            + _fmt_seconds(
                row[
                    "max_seconds"
                ]
            ).rjust(12)

            + (
                f"{row['second_search_rate'] * 100:.1f}%"
            ).rjust(
                14
            )
        )

    # --------------------------------------------------------
    # Capacity by depth
    # --------------------------------------------------------

    print()
    print(
        "CAPACITY ESTIMATE"
    )

    print(
        "  "
        + "Depth".ljust(8)
        + "1 worker".rjust(14)
        + "Workers".rjust(10)
        + "Ideal parallel".rjust(18)
    )

    for row in report[
        "capacity_estimates"
    ]:
        print(
            "  "
            + str(
                row["depth"]
            ).ljust(8)

            + (
                f"{row['one_worker_minutes']:.1f}m"
            ).rjust(
                14
            )

            + str(
                row[
                    "suggested_workers"
                ]
            ).rjust(
                10
            )

            + (
                f"{row['ideal_parallel_minutes']:.1f}m"
            ).rjust(
                18
            )
        )

    # --------------------------------------------------------
    # Qualification result
    # --------------------------------------------------------

    print()
    print(
        "QUALIFICATION"
    )

    target = report[
        "target_p95_seconds"
    ]

    suggestion = report[
        "suggested_depth"
    ]

    print(
        "  Basis         :",
        "full Lucas pipeline P95",
    )

    print(
        "  P95 target    :",
        f"{target:.2f}s",
    )

    if suggestion is None:
        print(
            "  Suggested     :",
            "below tested depth target",
        )
    else:
        print(
            "  Suggested     :",
            f"depth {suggestion}",
        )

    print(
        "=" * 76
    )


def default_pgn():
    candidate = Path(
        "tests/golden/"
        "tre_2026_game_1.pgn"
    )

    if candidate.exists():
        return candidate

    return None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cgm-bench",
        description=(
            "Benchmark and qualify compute "
            "platforms for ChessGrandmaster."
        ),
    )

    sub = parser.add_subparsers(
        dest="command",
        required=True,
    )

    qualify = sub.add_parser(
        "qualify",
        help=(
            "Measure fixed work, time-to-depth, "
            "and Lucas pipeline performance."
        ),
    )

    qualify.add_argument(
        "--pgn",
        type=Path,
        help=(
            "Benchmark PGN. Defaults to the "
            "repository golden game when present."
        ),
    )

    qualify.add_argument(
        "--engine",
        type=Path,
        help=(
            "Stockfish binary. Defaults to "
            "CGM_STOCKFISH/PATH."
        ),
    )

    qualify.add_argument(
        "--samples",
        type=int,
        default=12,
        help=(
            "Number of sampled positions "
            "(default: 12)."
        ),
    )

    qualify.add_argument(
        "--fixed-nodes",
        type=int,
        default=1_000_000,
        help=(
            "Fixed-work node budget "
            "(default: 1000000)."
        ),
    )

    qualify.add_argument(
        "--depths",
        type=parse_depths,
        default=[
            18,
            19,
            20,
        ],
        help=(
            "Comma-separated depth tests "
            "(default: 18,19,20)."
        ),
    )

    qualify.add_argument(
        "--hash-mb",
        type=int,
        default=256,
        help=(
            "Stockfish hash in MB "
            "(default: 256)."
        ),
    )

    qualify.add_argument(
        "--target-p95",
        type=float,
        default=3.0,
        help=(
            "Operational P95 time target "
            "for depth recommendation "
            "(default: 3.0 seconds)."
        ),
    )

    qualify.add_argument(
        "--tournament-moves",
        type=int,
        default=14_000,
        help=(
            "Move count used for capacity estimate "
            "(default: 14000)."
        ),
    )

    qualify.add_argument(
        "--json-out",
        type=Path,
        help=(
            "Optional JSON report output path."
        ),
    )

    return parser


def main(
    argv=None,
):
    args = build_parser().parse_args(
        argv
    )

    if args.command != "qualify":
        raise RuntimeError(
            f"Unknown command: {args.command}"
        )

    if args.samples <= 0:
        raise SystemExit(
            "--samples must be positive"
        )

    if args.fixed_nodes <= 0:
        raise SystemExit(
            "--fixed-nodes must be positive"
        )

    if args.hash_mb <= 0:
        raise SystemExit(
            "--hash-mb must be positive"
        )

    if args.target_p95 <= 0:
        raise SystemExit(
            "--target-p95 must be positive"
        )

    if args.tournament_moves <= 0:
        raise SystemExit(
            "--tournament-moves must be positive"
        )

    pgn = (
        args.pgn
        or default_pgn()
    )

    if pgn is None:
        raise SystemExit(
            "No benchmark PGN supplied and "
            "repository golden PGN was not found. "
            "Use --pgn."
        )

    if not pgn.exists():
        raise SystemExit(
            f"Benchmark PGN not found: {pgn}"
        )

    engine = resolve_installed_engine(
        args.engine
    )

    report = build_report(
        pgn_path=pgn,
        engine_path=engine,
        samples=args.samples,
        fixed_nodes=args.fixed_nodes,
        depths=args.depths,
        hash_mb=args.hash_mb,
        target_p95_seconds=args.target_p95,
        tournament_moves=args.tournament_moves,
    )

    print_report(
        report
    )

    if args.json_out:
        args.json_out.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        args.json_out.write_text(
            json.dumps(
                report,
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        print(
            "JSON report:",
            args.json_out,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
