import json
import sqlite3

import pytest

from chessgrandmaster.b2.json_codec import canonical_json_bytes
from chessgrandmaster.b2.registry import Registry


EXPECTED_TABLES = {
    "registry_meta",
    "sources",
    "tournaments",
    "source_tournaments",
    "source_files",
    "download_attempts",
    "canonical_games",
    "game_occurrences",
    "game_metadata_conflicts",
    "tournament_revisions",
    "tournament_games",
}


def rows_for(registry, query, parameters=()):
    with registry._connect() as connection:
        return connection.execute(query, parameters).fetchall()


def test_registry_schema_is_idempotent_and_foreign_keys_are_enabled(tmp_path):
    path = tmp_path / "registry.sqlite"
    registry = Registry(path)

    registry.ensure_schema()
    registry.ensure_schema()

    table_names = {
        row[0]
        for row in rows_for(
            registry,
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )
    }
    assert EXPECTED_TABLES <= table_names
    assert rows_for(registry, "PRAGMA user_version")[0][0] == 1
    assert rows_for(registry, "PRAGMA foreign_keys")[0][0] == 1
    assert rows_for(
        registry,
        "SELECT value FROM registry_meta WHERE key = 'schema_version'",
    )[0][0] == "1"


def test_registry_reuses_sources_and_exact_identities(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")

    source_id = registry.upsert_source(
        name="chess-results",
        base_url="https://s3.chess-results.com",
        priority=50,
    )
    same_source_id = registry.upsert_source(
        name="chess-results",
        base_url="https://s3.chess-results.com",
        priority=50,
    )
    tournament_id = registry.upsert_tournament(
        slug="cr-tnr-one",
        name="Example",
        status="DISCOVERED",
    )
    source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id="tnr1450909",
        source_url="https://example.invalid/tnr1450909.aspx",
    )
    same_source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id="tnr1450909",
        source_url="https://example.invalid/tnr1450909.aspx",
    )

    game_kwargs = {
        "fingerprint_version": "game_fingerprint_v1",
        "fingerprint": "a" * 64,
        "variant": "standard",
        "initial_fen": "start-fen",
        "mainline_uci": ("e2e4", "e7e5"),
        "ply_count": 2,
        "canonical_headers": {"Event": "Example"},
    }
    canonical_game_id = registry.upsert_canonical_game(**game_kwargs)
    same_canonical_game_id = registry.upsert_canonical_game(**game_kwargs)

    source_file_id = registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key="raw/a/original.pgn",
        filename="original.pgn",
        sha256="b" * 64,
        byte_size=10,
    )
    same_source_file_id = registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key="raw/a/original.pgn",
        filename="original.pgn",
        sha256="b" * 64,
        byte_size=10,
    )

    assert same_source_id == source_id
    assert same_source_tournament_id == source_tournament_id
    assert same_canonical_game_id == canonical_game_id
    assert same_source_file_id == source_file_id
    assert rows_for(registry, "SELECT COUNT(*) FROM sources")[0][0] == 1
    assert rows_for(registry, "SELECT COUNT(*) FROM source_tournaments")[0][0] == 1
    assert rows_for(registry, "SELECT COUNT(*) FROM canonical_games")[0][0] == 1
    assert rows_for(registry, "SELECT COUNT(*) FROM source_files")[0][0] == 1


def test_occurrences_preserve_conflicting_metadata_and_revision_membership(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    source_id = registry.upsert_source("fixture", "https://example.invalid", priority=20)
    tournament_a = registry.upsert_tournament("tournament-a", name="A")
    tournament_b = registry.upsert_tournament("tournament-b", name="B")
    source_tournament_a = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_a,
        external_id="section-a",
        source_url="https://example.invalid/a",
    )
    source_tournament_b = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_b,
        external_id="section-b",
        source_url="https://example.invalid/b",
    )
    source_file_a = registry.record_source_file(
        source_tournament_id=source_tournament_a,
        object_key="raw/a/original.pgn",
        filename="a.pgn",
        sha256="c" * 64,
        byte_size=20,
    )
    source_file_b = registry.record_source_file(
        source_tournament_id=source_tournament_b,
        object_key="raw/b/original.pgn",
        filename="b.pgn",
        sha256="d" * 64,
        byte_size=20,
    )
    game_id = registry.upsert_canonical_game(
        fingerprint_version="game_fingerprint_v1",
        fingerprint="e" * 64,
        variant="standard",
        initial_fen="start-fen",
        mainline_uci=("e2e4", "e7e5"),
        ply_count=2,
    )

    occurrence_a = registry.record_occurrence(
        canonical_game_id=game_id,
        tournament_id=tournament_a,
        source_file_id=source_file_a,
        source_game_index=1,
        raw_headers={"Result": "1-0", "White": "Nguyen A"},
        raw_pgn_object_key="raw/a/original.pgn",
    )
    occurrence_b = registry.record_occurrence(
        canonical_game_id=game_id,
        tournament_id=tournament_b,
        source_file_id=source_file_b,
        source_game_index=1,
        raw_headers={"Result": "0-1", "White": "NGUYEN, A"},
        raw_pgn_object_key="raw/b/original.pgn",
    )
    assert registry.record_occurrence(
        canonical_game_id=game_id,
        tournament_id=tournament_a,
        source_file_id=source_file_a,
        source_game_index=1,
        raw_headers={"Result": "1-0", "White": "Nguyen A"},
        raw_pgn_object_key="raw/a/original.pgn",
    ) == occurrence_a
    assert occurrence_a != occurrence_b
    assert rows_for(registry, "SELECT COUNT(*) FROM game_occurrences")[0][0] == 2

    registry.replace_metadata_conflicts(
        game_id,
        {"Result": ["0-1", "1-0"], "White": ["NGUYEN, A", "Nguyen A"]},
    )
    first_conflicts = rows_for(
        registry,
        "SELECT field, values_json FROM game_metadata_conflicts "
        "ORDER BY field",
    )
    registry.replace_metadata_conflicts(
        game_id,
        {"White": ["Nguyen A", "NGUYEN, A"], "Result": ["1-0", "0-1"]},
    )
    second_conflicts = rows_for(
        registry,
        "SELECT field, values_json FROM game_metadata_conflicts "
        "ORDER BY field",
    )

    assert [(row[0], row[1]) for row in first_conflicts] == [
        ("Result", canonical_json_bytes(["0-1", "1-0"]).decode("utf-8")),
        ("White", canonical_json_bytes(["NGUYEN, A", "Nguyen A"]).decode("utf-8")),
    ]
    assert [(row[0], row[1]) for row in second_conflicts] == [
        (row[0], row[1]) for row in first_conflicts
    ]
    assert json.loads(first_conflicts[0][1]) == ["0-1", "1-0"]

    revision_a, revision_a_number = registry.create_or_get_revision(
        tournament_id=tournament_a,
        canonical_sha256="f" * 64,
        canonicalization_policy="canonical_pgn_v1",
    )
    same_revision_a, same_revision_a_number = registry.create_or_get_revision(
        tournament_id=tournament_a,
        canonical_sha256="f" * 64,
        canonicalization_policy="canonical_pgn_v1",
    )
    revision_b, revision_b_number = registry.create_or_get_revision(
        tournament_id=tournament_b,
        canonical_sha256="f" * 64,
        canonicalization_policy="canonical_pgn_v1",
    )

    registry.set_revision_games(revision_a, [game_id, game_id])
    registry.set_revision_games(revision_a, [game_id])
    registry.set_revision_games(revision_b, [game_id])

    revision_a_games = registry.get_revision_games(revision_a)
    revision_b_games = registry.get_revision_games(revision_b)
    assert (same_revision_a, same_revision_a_number) == (revision_a, revision_a_number)
    assert revision_a_number == revision_b_number == 1
    assert [row["canonical_game_id"] for row in revision_a_games] == [game_id]
    assert [row["ordinal"] for row in revision_a_games] == [1]
    assert [row["canonical_game_id"] for row in revision_b_games] == [game_id]
    assert rows_for(registry, "SELECT COUNT(*) FROM tournament_games")[0][0] == 2


def test_tournament_status_is_validated_and_does_not_regress(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("status-test")

    registry.set_tournament_status(tournament_id, "DOWNLOADED")
    registry.set_tournament_status(tournament_id, "CANONICALIZED")
    registry.upsert_tournament("status-test", status="DISCOVERED")

    assert registry.get_tournament_status(tournament_id) == "CANONICALIZED"
    with pytest.raises(ValueError):
        registry.set_tournament_status(tournament_id, "NOT_A_STATE")
    with pytest.raises(ValueError):
        registry.set_tournament_status(tournament_id, "VALIDATED")
