import pytest

from chessgrandmaster.b2.sharding import ShardGame, plan_shards


def game(index: int, plies: int) -> ShardGame:
    return ShardGame(index, f"{index:064x}", index, plies, f"[Event \"G{index}\"]\n\n*\n")


def test_shards_never_split_or_duplicate_games():
    games = [game(1, 1800), game(2, 1700), game(3, 900), game(4, 600)]

    plans = plan_shards(games, target_plies=3000)
    ids = [item.canonical_game_id for plan in plans for item in plan.games]

    assert sorted(ids) == [1, 2, 3, 4]
    assert len(ids) == len(set(ids))
    assert all(plan.total_plies == sum(item.ply_count for item in plan.games) for plan in plans)


def test_sharding_is_deterministic_and_preserves_ordinal_order_within_bins():
    games = [game(4, 600), game(2, 1700), game(1, 1800), game(3, 900)]

    first = plan_shards(games, 3000)
    second = plan_shards(games, 3000)

    assert first == second
    assert [item.ordinal for plan in first for item in plan.games] == [
        item.ordinal for plan in first for item in sorted(plan.games, key=lambda value: value.ordinal)
    ]


def test_sharding_balance_is_bounded_by_largest_game():
    plans = plan_shards([game(1, 1000), game(2, 900), game(3, 800), game(4, 700)], 1500)
    loads = [plan.total_plies for plan in plans]

    assert max(loads) - min(loads) <= 1000


def test_oversized_game_is_kept_whole_and_nonpositive_target_is_rejected():
    plans = plan_shards([game(1, 5000), game(2, 1)], target_plies=3000)

    assert sum(plan.total_plies for plan in plans) == 5001
    assert any(plan.total_plies == 5000 for plan in plans)
    with pytest.raises(ValueError, match="target"):
        plan_shards([game(1, 1)], target_plies=0)
