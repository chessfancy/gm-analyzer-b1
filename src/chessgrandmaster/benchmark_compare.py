import argparse
import json
from pathlib import Path


SCHEMA_VERSION = 2
DEPTH_BASIS = "full_lucas_pipeline_p95"


def load_report(path):
    path = Path(path)

    try:
        data = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except OSError as exc:
        raise ValueError(
            f"unable to read report: {path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid JSON report: {path}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"report must contain a JSON object: {path}"
        )

    return data


def _required(report, key):
    if key not in report:
        raise ValueError(
            f"missing report field: {key}"
        )

    return report[key]


def _nested(report, section, key):
    value = _required(
        report,
        section,
    )

    if not isinstance(value, dict):
        raise ValueError(
            f"report field must be an object: {section}"
        )

    if key not in value:
        raise ValueError(
            f"missing report field: {section}.{key}"
        )

    return value[key]


def _depth_map(report, section):
    rows = _required(
        report,
        section,
    )

    if not isinstance(rows, list) or not rows:
        raise ValueError(
            f"report field must be a non-empty list: {section}"
        )

    result = {}

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(
                f"report rows must be objects: {section}"
            )

        try:
            depth = int(row["depth"])
            p95 = float(row["p95_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid depth row in {section}"
            ) from exc

        if depth in result:
            raise ValueError(
                f"duplicate depth {depth} in {section}"
            )

        result[depth] = p95

    return result


def _tournament_moves(report):
    rows = _required(
        report,
        "capacity_estimates",
    )

    if not isinstance(rows, list) or not rows:
        raise ValueError(
            "capacity_estimates must be a non-empty list"
        )

    values = {
        int(row["tournament_moves"])
        for row in rows
        if isinstance(row, dict)
        and "tournament_moves" in row
    }

    if len(values) != 1:
        raise ValueError(
            "capacity_estimates.tournament_moves must be consistent"
        )

    return values.pop()


def _contract(report):
    schema = _required(
        report,
        "schema_version",
    )

    if schema != SCHEMA_VERSION:
        raise ValueError(
            "schema_version must be 2"
        )

    basis = _required(
        report,
        "suggested_depth_basis",
    )

    if basis != DEPTH_BASIS:
        raise ValueError(
            "suggested_depth_basis must be "
            "full_lucas_pipeline_p95"
        )

    raw_depths = sorted(
        _depth_map(
            report,
            "depth_benchmarks",
        )
    )

    pipeline_depths = sorted(
        _depth_map(
            report,
            "pipeline_benchmarks",
        )
    )

    if raw_depths != pipeline_depths:
        raise ValueError(
            "depth_benchmarks and pipeline_benchmarks "
            "must test the same depths"
        )

    return {
        "schema_version": schema,
        "engine.uci_name": _nested(
            report,
            "engine",
            "uci_name",
        ),
        "engine.threads": int(
            _nested(
                report,
                "engine",
                "threads",
            )
        ),
        "engine.hash_mb": int(
            _nested(
                report,
                "engine",
                "hash_mb",
            )
        ),
        "engine.multipv": int(
            _nested(
                report,
                "engine",
                "multipv",
            )
        ),
        "fixed_work.nodes": int(
            _nested(
                report,
                "fixed_work",
                "nodes",
            )
        ),
        "sample_count": int(
            _required(
                report,
                "sample_count",
            )
        ),
        "sample_positions": _required(
            report,
            "sample_positions",
        ),
        "tested_depths": pipeline_depths,
        "target_p95_seconds": float(
            _required(
                report,
                "target_p95_seconds",
            )
        ),
        "suggested_depth_basis": basis,
        "tournament_moves": _tournament_moves(
            report
        ),
    }


def _validate_same_contract(
    reference,
    candidate,
    label,
):
    for key, expected in reference.items():
        actual = candidate[key]

        if actual != expected:
            raise ValueError(
                f"incompatible report {label!r}: "
                f"{key} differs "
                f"({actual!r} != {expected!r})"
            )


def build_comparison(named_reports):
    named_reports = list(
        named_reports
    )

    if len(named_reports) < 2:
        raise ValueError(
            "compare requires at least two reports"
        )

    reference_label, reference_report = (
        named_reports[0]
    )

    reference_contract = _contract(
        reference_report
    )

    rows = []

    for label, report in named_reports:
        contract = _contract(
            report
        )

        _validate_same_contract(
            reference_contract,
            contract,
            label,
        )

        pipeline = _depth_map(
            report,
            "pipeline_benchmarks",
        )

        fixed_nps = float(
            _nested(
                report,
                "fixed_work",
                "median_nps",
            )
        )

        suggested = _required(
            report,
            "suggested_depth",
        )

        if suggested is not None:
            suggested = int(
                suggested
            )

        rows.append({
            "platform": str(label),
            "hostname": str(
                _nested(
                    report,
                    "hardware",
                    "hostname",
                )
            ),
            "fixed_nps": fixed_nps,
            "pipeline_p95": pipeline,
            "suggested_depth": suggested,
            "binary_sha256": str(
                _nested(
                    report,
                    "engine",
                    "binary_sha256",
                )
            ),
        })

    return {
        "engine": reference_contract[
            "engine.uci_name"
        ],
        "threads": reference_contract[
            "engine.threads"
        ],
        "hash_mb": reference_contract[
            "engine.hash_mb"
        ],
        "multipv": reference_contract[
            "engine.multipv"
        ],
        "fixed_nodes": reference_contract[
            "fixed_work.nodes"
        ],
        "sample_count": reference_contract[
            "sample_count"
        ],
        "depths": reference_contract[
            "tested_depths"
        ],
        "target_p95_seconds": reference_contract[
            "target_p95_seconds"
        ],
        "tournament_moves": reference_contract[
            "tournament_moves"
        ],
        "basis": DEPTH_BASIS,
        "reference_platform": str(
            reference_label
        ),
        "rows": rows,
    }


def _nps_header(nodes):
    if nodes == 1_000_000:
        return "1M NPS"

    return f"{nodes:,} NPS"


def _suggested_label(value, depths):
    if value is None:
        return f"<D{min(depths)}"

    return f"D{value}"


def format_comparison_table(comparison):
    depths = comparison[
        "depths"
    ]

    headers = [
        "Platform",
        _nps_header(
            comparison[
                "fixed_nodes"
            ]
        ),
        *[
            f"D{depth} P95"
            for depth in depths
        ],
        "Suggested",
    ]

    body = []

    for row in comparison[
        "rows"
    ]:
        body.append([
            row["platform"],
            f"{row['fixed_nps']:,.0f}",
            *[
                f"{row['pipeline_p95'][depth]:.3f}s"
                for depth in depths
            ],
            _suggested_label(
                row[
                    "suggested_depth"
                ],
                depths,
            ),
        ])

    widths = [
        max(
            len(headers[index]),
            *(
                len(row[index])
                for row in body
            ),
        )
        for index in range(
            len(headers)
        )
    ]

    def render(row):
        cells = []

        for index, value in enumerate(row):
            if index == 0:
                cells.append(
                    value.ljust(
                        widths[index]
                    )
                )
            else:
                cells.append(
                    value.rjust(
                        widths[index]
                    )
                )

        return "  ".join(
            cells
        )

    lines = [
        render(headers),
        render([
            "-" * width
            for width in widths
        ]),
        *[
            render(row)
            for row in body
        ],
    ]

    return "\n".join(
        lines
    )


def print_comparison(comparison):
    print()
    print(
        "CHESSGRANDMASTER QUALIFICATION COMPARISON"
    )
    print(
        "Engine              :",
        comparison["engine"],
    )
    print(
        "Samples             :",
        comparison["sample_count"],
    )
    print(
        "Fixed work          :",
        f"{comparison['fixed_nodes']:,} nodes",
    )
    print(
        "Search config       :",
        (
            f"Threads={comparison['threads']}, "
            f"Hash={comparison['hash_mb']} MB, "
            f"MultiPV={comparison['multipv']}"
        ),
    )
    print(
        "Pipeline P95 target :",
        f"{comparison['target_p95_seconds']:.2f}s",
    )
    print(
        "Basis               :",
        "full Lucas pipeline P95",
    )
    print()
    print(
        format_comparison_table(
            comparison
        )
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cgm-bench compare",
        description=(
            "Compare compatible ChessGrandmaster "
            "qualification JSON reports."
        ),
    )

    parser.add_argument(
        "reports",
        type=Path,
        nargs="+",
        help=(
            "Qualification JSON reports. "
            "The file stem is used as the platform label."
        ),
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(
        argv
    )

    if len(args.reports) < 2:
        parser.error(
            "at least two JSON reports are required"
        )

    try:
        named_reports = [
            (
                path.stem,
                load_report(path),
            )
            for path in args.reports
        ]

        comparison = build_comparison(
            named_reports
        )
    except ValueError as exc:
        parser.error(
            str(exc)
        )

    print_comparison(
        comparison
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
