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
