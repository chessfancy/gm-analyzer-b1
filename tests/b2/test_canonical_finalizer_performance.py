from __future__ import annotations

import hashlib
import io

import chess.pgn

from chessgrandmaster.b2.canonicalize import (
    ProcessedGameResult,
    finalize_processed_games,
)
from chessgrandmaster.b2.identity import GameIdentity
from chessgrandmaster.b2.registry import Registry


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


def _identity(moves: tuple[str, ...]) -> GameIdentity:
    fingerprint = hashlib.sha256(
        "\n".join(("standard", START_FEN, *moves)).encode("utf-8")
    ).hexdigest()
    return GameIdentity(
        fingerprint_version="game_fingerprint_v1",
        fingerprint=fingerprint,
        variant="standard",
        initial_fen=START_FEN,
        mainline_uci=moves,
        ply_count=len(moves),
    )


def _source_file(
    registry: Registry,
    tournament_id: int,
    source_name: str,
    priority: int,
    external_id: str,
) -> int:
    source_id = registry.upsert_source(
        source_name,
        "https://example.invalid",
        priority=priority,
    )
    source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id=external_id,
    )
    raw_bytes = f"{source_name}-{external_id}".encode("ascii")
    return registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key=f"raw/{external_id}.pgn",
        filename=f"{external_id}.pgn",
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        byte_size=len(raw_bytes),
    )


class CandidateCountingRegistry(Registry):
    def __init__(self, path):
        self.candidate_calls: list[tuple[int, int | None]] = []
        super().__init__(path)

    def list_valid_occurrence_candidates(
        self,
        canonical_game_id: int,
        *,
        tournament_id: int | None = None,
    ) -> list[dict[str, object]]:
        self.candidate_calls.append((int(canonical_game_id), tournament_id))
        return super().list_valid_occurrence_candidates(
            canonical_game_id,
            tournament_id=tournament_id,
        )


def test_finalizer_reuses_global_candidates_for_local_selection(tmp_path):
    registry = CandidateCountingRegistry(tmp_path / "registry.sqlite")
    historical_tournament = registry.upsert_tournament("historical")
    target_tournament = registry.upsert_tournament("target")
    historical_file = _source_file(
        registry,
        historical_tournament,
        "official",
        priority=100,
        external_id="historical",
    )
    target_file = _source_file(
        registry,
        target_tournament,
        "target",
        priority=10,
        external_id="target",
    )
    target_preferred_file = _source_file(
        registry,
        target_tournament,
        "target-preferred",
        priority=20,
        external_id="target-preferred",
    )

    identities = [
        _identity(("e2e4", "e7e5")),
        _identity(("d2d4", "d7d5")),
    ]
    canonical_game_ids = [
        registry.upsert_canonical_game(identity) for identity in identities
    ]
    historical_occurrence_ids = []
    for index, canonical_game_id in enumerate(canonical_game_ids, start=1):
        historical_occurrence_ids.append(
            registry.record_occurrence(
                canonical_game_id=canonical_game_id,
                tournament_id=historical_tournament,
                source_file_id=historical_file,
                source_game_index=index,
                raw_headers={
                    "Event": "Official Event",
                    "White": f"Official White {index}",
                    "Black": f"Official Black {index}",
                    "Result": "1-0",
                },
                raw_pgn_object_key="raw/historical.pgn",
            )
        )

    target_preferred_occurrence_ids = []
    for index, canonical_game_id in enumerate(canonical_game_ids, start=1):
        target_preferred_occurrence_ids.append(
            registry.record_occurrence(
                canonical_game_id=canonical_game_id,
                tournament_id=target_tournament,
                source_file_id=target_preferred_file,
                source_game_index=index,
                raw_headers={
                    "Event": "Target Preferred Event",
                    "White": f"Preferred White {index}",
                    "Black": f"Preferred Black {index}",
                    "Result": "1/2-1/2",
                },
                raw_pgn_object_key="raw/target-preferred.pgn",
            )
        )

    processed = [
        ProcessedGameResult(
            source_game_index=index,
            raw_headers={
                "Event": "Target Event",
                "White": f"Target White {index}",
                "Black": f"Target Black {index}",
                "Result": "0-1",
            },
            identity=identity,
            is_valid=True,
            parse_count=1,
        )
        for index, identity in enumerate(identities, start=1)
    ]
    result = finalize_processed_games(
        registry,
        target_tournament,
        target_file,
        "raw/target.pgn",
        processed,
        tmp_path / "canonical.pgn",
    )

    assert result.game_count == 2
    assert len(registry.candidate_calls) == len(identities)
    assert all(tournament_id is None for _, tournament_id in registry.candidate_calls)

    revision_games = registry.get_revision_games(result.revision_id)
    selected_occurrence_ids = [
        int(row["selected_occurrence_id"]) for row in revision_games
    ]
    assert selected_occurrence_ids == target_preferred_occurrence_ids
    assert all(
        selected_id not in historical_occurrence_ids
        for selected_id in selected_occurrence_ids
    )
    assert all(
        registry.get_occurrences(canonical_game_id)[-1]["tournament_id"] == target_tournament
        for canonical_game_id in canonical_game_ids
    )

    global_rows = [
        registry.get_canonical_game_by_id(canonical_game_id)
        for canonical_game_id in canonical_game_ids
    ]
    assert all(
        int(row["canonical_occurrence_id"]) in historical_occurrence_ids
        for row in global_rows
    )

    stream = io.StringIO((tmp_path / "canonical.pgn").read_text(encoding="utf-8"))
    exported = []
    while game := chess.pgn.read_game(stream):
        exported.append(game)
    assert [game.headers["Event"] for game in exported] == [
        "Target Preferred Event",
        "Target Preferred Event",
    ]
