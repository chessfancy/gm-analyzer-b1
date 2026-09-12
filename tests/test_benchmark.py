from chessgrandmaster import benchmark


def test_percentile_interpolates():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]

    assert benchmark.percentile(
        values,
        50,
    ) == 3.0

    assert benchmark.percentile(
        values,
        100,
    ) == 5.0


def test_sample_positions_is_even_and_keeps_tactical_ply():
    records = [
        {
            "game_index": 1,
            "ply": ply,
            "fen": f"fen-{ply}",
            "played_uci": "a2a3",
            "san": "a3",
        }
        for ply in range(1, 84)
    ]

    selected = benchmark.sample_positions(
        records,
        count=10,
        skip_opening=8,
    )

    assert len(selected) == 10

    plies = [
        row["ply"]
        for row in selected
    ]

    assert 42 in plies

    assert min(plies) > 8

    assert plies == sorted(plies)


def test_recommend_depth_chooses_highest_depth_under_target():
    rows = [
        {
            "depth": 18,
            "p95_seconds": 0.8,
        },
        {
            "depth": 19,
            "p95_seconds": 1.7,
        },
        {
            "depth": 20,
            "p95_seconds": 3.8,
        },
    ]

    assert benchmark.recommend_depth(
        rows,
        target_p95_seconds=3.0,
    ) == 19


def test_recommend_depth_returns_none_if_even_lowest_is_too_slow():
    rows = [
        {
            "depth": 18,
            "p95_seconds": 5.0,
        },
        {
            "depth": 19,
            "p95_seconds": 9.0,
        },
    ]

    assert (
        benchmark.recommend_depth(
            rows,
            target_p95_seconds=3.0,
        )
        is None
    )


def test_parse_depths():
    assert benchmark.parse_depths(
        "18,19,20"
    ) == [18, 19, 20]


def test_build_report_recommends_from_full_pipeline_p95(
    monkeypatch,
    tmp_path,
):
    from chessgrandmaster import benchmark

    monkeypatch.setattr(
        benchmark,
        "hardware_info",
        lambda: {
            "hostname": "test",
            "platform": "test",
            "machine": "x86_64",
            "processor": "",
            "logical_cpu": 2,
            "cgroup_cpu_quota": None,
            "effective_cpu": 2.0,
            "suggested_workers": 2,
            "memory_bytes": 8 * 1024**3,
            "memory_gib": 8.0,
        },
    )

    monkeypatch.setattr(
        benchmark,
        "verify_engine_binary",
        lambda path: {
            "uci_name": "Stockfish 19",
            "path": str(path),
            "binary_sha256": "abc123",
        },
    )

    records = [
        {
            "game_index": 1,
            "ply": ply,
            "fen": "dummy",
            "played_uci": "a2a3",
            "san": "a3",
        }
        for ply in range(10, 22)
    ]

    monkeypatch.setattr(
        benchmark,
        "load_pgn_positions",
        lambda path: records,
    )

    monkeypatch.setattr(
        benchmark,
        "sample_positions",
        lambda rows, count: rows[:count],
    )

    monkeypatch.setattr(
        benchmark,
        "run_fixed_work_phase",
        lambda *args, **kwargs: {
            "summary": {
                "samples": 3,
                "median_seconds": 1.0,
                "p95_seconds": 1.1,
                "max_seconds": 1.2,
                "mean_seconds": 1.0,
                "median_nodes": 1_000_000,
                "median_nps": 1_000_000,
                "median_depth": 19,
            },
        },
    )

    raw_p95 = {
        18: 0.8,
        19: 1.4,
        20: 2.2,
    }

    def fake_depth(
        engine_path,
        positions,
        *,
        depth,
        hash_mb,
    ):
        return {
            "depth": depth,
            "summary": {
                "samples": 3,
                "median_seconds": raw_p95[depth] * 0.7,
                "p95_seconds": raw_p95[depth],
                "max_seconds": raw_p95[depth] * 1.1,
                "mean_seconds": raw_p95[depth] * 0.75,
                "median_nodes": depth * 100_000,
                "median_nps": 1_000_000,
                "median_depth": depth,
            },
        }

    monkeypatch.setattr(
        benchmark,
        "run_depth_phase",
        fake_depth,
    )

    pipeline_p95 = {
        18: 1.7,
        19: 2.9,
        20: 4.8,
    }

    pipeline_mean = {
        18: 1.0,
        19: 1.5,
        20: 2.0,
    }

    pipeline_calls = []

    def fake_pipeline(
        engine_path,
        positions,
        *,
        depth,
        hash_mb,
    ):
        pipeline_calls.append(depth)

        return {
            "depth": depth,
            "samples": 3,
            "mean_seconds": pipeline_mean[depth],
            "median_seconds": pipeline_mean[depth] * 0.9,
            "p95_seconds": pipeline_p95[depth],
            "max_seconds": pipeline_p95[depth] * 1.1,
            "second_searches": 2,
            "second_search_rate": 2 / 3,
            "rows": [],
        }

    monkeypatch.setattr(
        benchmark,
        "run_pipeline_phase",
        fake_pipeline,
    )

    report = benchmark.build_report(
        pgn_path=tmp_path / "test.pgn",
        engine_path=tmp_path / "stockfish",
        samples=3,
        fixed_nodes=1_000_000,
        depths=[18, 19, 20],
        hash_mb=256,
        target_p95_seconds=3.0,
        tournament_moves=1200,
    )

    # Raw D20 fits under 3 sec, but full Lucas D20 does not.
    # Recommendation MUST therefore be D19.
    assert report["suggested_depth"] == 19

    assert (
        report["suggested_depth_basis"]
        == "full_lucas_pipeline_p95"
    )

    assert pipeline_calls == [
        18,
        19,
        20,
    ]

    assert [
        row["depth"]
        for row in report["pipeline_benchmarks"]
    ] == [
        18,
        19,
        20,
    ]

    capacities = {
        row["depth"]: row
        for row in report["capacity_estimates"]
    }

    # D19:
    # 1.5 sec/ply * 1200 = 1800 sec = 30 min.
    assert capacities[19]["one_worker_minutes"] == 30.0

    # Two effective workers -> ideal 15 min.
    assert capacities[19]["ideal_parallel_minutes"] == 15.0
