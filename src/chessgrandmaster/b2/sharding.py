"""Deterministic whole-game shard planning for B2b workloads."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


SHARDING_POLICY = "whole_game_plies_v1"


@dataclass(frozen=True)
class ShardGame:
    """One complete canonical game assigned to a shard."""

    canonical_game_id: int
    fingerprint: str
    ordinal: int
    ply_count: int
    pgn_text: str

    def __post_init__(self) -> None:
        if int(self.canonical_game_id) < 1:
            raise ValueError("canonical_game_id must be positive")
        if not self.fingerprint:
            raise ValueError("fingerprint must not be empty")
        if int(self.ordinal) < 1:
            raise ValueError("ordinal must be positive")
        if int(self.ply_count) < 0:
            raise ValueError("ply_count must not be negative")
        if not isinstance(self.pgn_text, str) or not self.pgn_text:
            raise ValueError("pgn_text must be a non-empty string")


@dataclass(frozen=True)
class ShardPlan:
    """One deterministic bin of complete games."""

    shard_index: int
    games: tuple[ShardGame, ...]
    total_plies: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "games", tuple(self.games))
        if int(self.shard_index) < 0:
            raise ValueError("shard_index must not be negative")
        expected = sum(int(game.ply_count) for game in self.games)
        if int(self.total_plies) != expected:
            raise ValueError("total_plies must match the games in the shard")

    @property
    def game_count(self) -> int:
        return len(self.games)

    @property
    def canonical_game_fingerprints(self) -> tuple[str, ...]:
        return tuple(game.fingerprint for game in self.games)


def plan_shards(
    games: Sequence[ShardGame],
    target_plies: int = 3000,
) -> tuple[ShardPlan, ...]:
    """Assign complete games to deterministic LPT bins near ``target_plies``.

    The target controls the number of bins; an oversized game is never split.
    Candidate assignment is sorted by descending game size, then stable
    tournament ordinal and fingerprint.  Each game goes to the least-loaded
    bin, with shard index as the deterministic tie breaker.  Games in each
    resulting bin are restored to canonical tournament order for PGN output.
    """
    if int(target_plies) <= 0:
        raise ValueError("target_plies must be positive")
    materialized = tuple(games)
    if not materialized:
        return ()

    ordinals = [int(game.ordinal) for game in materialized]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("game ordinals must be unique")
    fingerprints = [str(game.fingerprint) for game in materialized]
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("game fingerprints must be unique")

    total_plies = sum(int(game.ply_count) for game in materialized)
    shard_count = max(
        1,
        min(len(materialized), math.ceil(total_plies / int(target_plies))),
    )
    bins: list[list[ShardGame]] = [[] for _ in range(shard_count)]
    loads = [0] * shard_count

    candidates = sorted(
        materialized,
        key=lambda game: (
            -int(game.ply_count),
            int(game.ordinal),
            str(game.fingerprint),
            int(game.canonical_game_id),
        ),
    )
    for game in candidates:
        shard_index = min(range(shard_count), key=lambda index: (loads[index], index))
        bins[shard_index].append(game)
        loads[shard_index] += int(game.ply_count)

    plans: list[ShardPlan] = []
    for shard_index, shard_games in enumerate(bins):
        ordered_games = tuple(
            sorted(
                shard_games,
                key=lambda game: (
                    int(game.ordinal),
                    str(game.fingerprint),
                    int(game.canonical_game_id),
                ),
            )
        )
        plans.append(ShardPlan(shard_index, ordered_games, loads[shard_index]))
    return tuple(plans)
