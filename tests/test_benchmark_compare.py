import json
from pathlib import Path

import pytest


def make_report(
    *,
    host="codespaces",
    engine="Stockfish 19",
    binary_sha="abc",
    hash_mb=256,
    sample_positions=None,
    pipeline_p95=None,
    suggested=18,
):
    if sample_positions is None:
        sample_positions = [
            {"game_index": 1, "ply": 10, "san": "Nf3"},
            {"game_index": 1, "ply": 42, "san": "Qxd5"},
        ]

    if pipeline_p95 is None:
        pipeline_p95 = {
            18: 1.36,
            19: 3.23,
            20: 4.84,
        }

    return {
        "schema_version": 2,
        "source_pgn": f"/different/{host}/golden.pgn",
        "sample_count": len(sample_positions),
        "sample_positions": sample_positions,
        "hardware": {
            "hostname": host,
            "effective_cpu": 2.0,
            "suggested_workers": 2,
        },
        "engine": {
            "uci_name": engine,
            "path": f"/{host}/stockfish",
            "binary_sha256": binary_sha,
            "threads": 1,
            "hash_mb": hash_mb,
            "multipv": 1,
        },
        "fixed_work": {
            "nodes": 1_000_000,
            "median_nps": 627_716,
            "median_seconds": 1.62,
            "p95_seconds": 1.88,
        },
        "depth_benchmarks": [
            {"depth": depth, "p95_seconds": value / 2}
            for depth, value in pipeline_p95.items()
        ],
        "pipeline_benchmarks": [
            {
                "depth": depth,
                "p95_seconds": value,
                "second_search_rate": 0.75,
            }
            for depth, value in pipeline_p95.items()
        ],
        "target_p95_seconds": 3.0,
        "suggested_depth": suggested,
        "suggested_depth_basis": "full_lucas_pipeline_p95",
        "capacity_estimates": [
            {
                "depth": depth,
                "tournament_moves": 14_000,
            }
            for depth in pipeline_p95
        ],
    }


def test_build_comparison_uses_full_pipeline_p95():
    from chessgrandmaster import benchmark_compare as compare

    rows = compare.build_comparison([
        ("codespaces", make_report()),
        (
            "deepnote",
            make_report(
                host="deepnote",
                binary_sha="different-binary",
                pipeline_p95={18: 0.9, 19: 1.8, 20: 3.4},
                suggested=19,
            ),
        ),
    ])

    assert rows["depths"] == [18, 19, 20]
    assert rows["rows"][0]["fixed_nps"] == 627_716
    assert rows["rows"][0]["pipeline_p95"][19] == 3.23
    assert rows["rows"][1]["pipeline_p95"][19] == 1.8
    assert rows["rows"][1]["suggested_depth"] == 19


def test_comparison_allows_different_binary_hash_and_source_path():
    from chessgrandmaster import benchmark_compare as compare

    result = compare.build_comparison([
        ("x86", make_report(binary_sha="x86")),
        ("arm", make_report(host="arm", binary_sha="arm")),
    ])

    assert len(result["rows"]) == 2


def test_comparison_rejects_engine_mismatch():
    from chessgrandmaster import benchmark_compare as compare

    with pytest.raises(ValueError, match="engine.uci_name"):
        compare.build_comparison([
            ("a", make_report()),
            ("b", make_report(engine="Stockfish 18")),
        ])


def test_comparison_rejects_corpus_mismatch():
    from chessgrandmaster import benchmark_compare as compare

    changed = [
        {"game_index": 1, "ply": 11, "san": "Nc3"},
        {"game_index": 1, "ply": 42, "san": "Qxd5"},
    ]

    with pytest.raises(ValueError, match="sample_positions"):
        compare.build_comparison([
            ("a", make_report()),
            ("b", make_report(sample_positions=changed)),
        ])


def test_comparison_rejects_search_config_mismatch():
    from chessgrandmaster import benchmark_compare as compare

    with pytest.raises(ValueError, match="engine.hash_mb"):
        compare.build_comparison([
            ("a", make_report()),
            ("b", make_report(hash_mb=128)),
        ])


def test_format_comparison_table_has_dynamic_depth_columns():
    from chessgrandmaster import benchmark_compare as compare

    comparison = compare.build_comparison([
        ("codespaces", make_report()),
        ("deepnote", make_report(host="deepnote", suggested=19)),
    ])

    text = compare.format_comparison_table(comparison)

    assert "Platform" in text
    assert "1M NPS" in text
    assert "D18 P95" in text
    assert "D19 P95" in text
    assert "D20 P95" in text
    assert "codespaces" in text
    assert "D18" in text
    assert "D19" in text


def test_main_reads_json_files_and_prints_table(tmp_path, capsys):
    from chessgrandmaster import benchmark_compare as compare

    a = tmp_path / "codespaces.json"
    b = tmp_path / "deepnote.json"

    a.write_text(json.dumps(make_report()), encoding="utf-8")
    b.write_text(
        json.dumps(make_report(host="deepnote", suggested=19)),
        encoding="utf-8",
    )

    assert compare.main([str(a), str(b)]) == 0

    output = capsys.readouterr().out
    assert "codespaces" in output
    assert "deepnote" in output
    assert "full Lucas pipeline P95" in output
