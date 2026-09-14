from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import shutil

import chess.pgn
import pytest

from chessgrandmaster.b2.canonicalize import (
    canonicalize_source_file,
)
from chessgrandmaster.b2.identity import identify_game
from chessgrandmaster.b2.registry import Registry


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _register_fixture(
    registry: Registry,
    tournament_id: int,
    raw_path: Path,
    *,
    source_name: str,
    source_priority: int,
    external_id: str,
) -> int:
    raw_bytes = raw_path.read_bytes()
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    source_id = registry.upsert_source(
        source_name,
        "https://example.invalid",
        priority=source_priority,
    )
    source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id=external_id,
        source_url=f"https://example.invalid/{external_id}",
    )
    return registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key=f"raw/{sha256}/original.pgn",
        filename=raw_path.name,
        sha256=sha256,
        byte_size=len(raw_bytes),
        content_type="application/x-chess-pgn",
    )


def _copy_fixture(tmp_path: Path, name: str, target: str) -> Path:
    raw_path = tmp_path / target
    shutil.copyfile(FIXTURES / name, raw_path)
    return raw_path


def _count(registry: Registry, table: str) -> int:
    with registry._connect() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _parse_games(path: Path) -> list[chess.pgn.Game]:
    games = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        while True:
            game = chess.pgn.read_game(stream)
            if game is None:
                break
            games.append(game)
    return games


def _revision_game_ids(registry: Registry, revision_id: int) -> list[int]:
    return [
        int(row["canonical_game_id"])
        for row in registry.get_revision_games(revision_id)
    ]


def test_exact_duplicates_preserve_occurrences_but_project_once(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("duplicate-tournament")
    raw_path = _copy_fixture(
        tmp_path,
        "duplicate_conflict_a.pgn",
        "duplicate_conflict_a.pgn",
    )
    source_file_id = _register_fixture(
        registry,
        tournament_id,
        raw_path,
        source_name="fixture-a",
        source_priority=10,
        external_id="fixture-a",
    )
    output_path = tmp_path / "canonical.pgn"

    result = canonicalize_source_file(
        registry,
        tournament_id,
        source_file_id,
        raw_path,
        output_path,
    )

    expected = (
        '[Event "Youth Open"]\n'
        '[Site "Hanoi"]\n'
        '[Date "2026.09.14"]\n'
        '[Round "1"]\n'
        '[White "Nguyen A"]\n'
        '[Black "Player B"]\n'
        '[Result "1-0"]\n'
        '\n'
        '1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0\n'
    ).encode("utf-8")
    assert result.tournament_id == tournament_id
    assert result.revision_id > 0
    assert result.revision_number == 1
    assert result.game_count == 1
    assert result.ply_count == 6
    assert result.source_game_count == 2
    assert result.valid_game_count == 2
    assert result.invalid_game_count == 0
    assert result.duplicate_occurrence_count == 1
    assert result.metadata_conflict_count == 0
    assert result.canonical_path == output_path
    assert output_path.read_bytes() == expected
    assert result.canonical_sha256 == hashlib.sha256(expected).hexdigest()
    assert _count(registry, "canonical_games") == 1
    assert _count(registry, "game_occurrences") == 2
    assert len(registry.get_revision_games(result.revision_id)) == 1
    assert len(_parse_games(output_path)) == 1
    assert not _parse_games(output_path)[0].errors


def test_conflicts_and_global_display_are_deterministic_but_local_headers_stay_local(
    tmp_path,
):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_a = registry.upsert_tournament("tournament-a")
    tournament_b = registry.upsert_tournament("tournament-b")
    raw_a = _copy_fixture(tmp_path, "duplicate_conflict_a.pgn", "a.pgn")
    raw_b = _copy_fixture(tmp_path, "duplicate_conflict_b.pgn", "b.pgn")
    source_file_a = _register_fixture(
        registry,
        tournament_a,
        raw_a,
        source_name="secondary",
        source_priority=10,
        external_id="section-a",
    )
    source_file_b = _register_fixture(
        registry,
        tournament_b,
        raw_b,
        source_name="official",
        source_priority=50,
        external_id="section-b",
    )

    result_a = canonicalize_source_file(
        registry,
        tournament_a,
        source_file_a,
        raw_a,
        tmp_path / "canonical-a.pgn",
    )
    result_b = canonicalize_source_file(
        registry,
        tournament_b,
        source_file_b,
        raw_b,
        tmp_path / "canonical-b.pgn",
    )

    revision_a_games = registry.get_revision_games(result_a.revision_id)
    revision_b_games = registry.get_revision_games(result_b.revision_id)
    game_a = int(revision_a_games[0]["canonical_game_id"])
    game_b = int(revision_b_games[0]["canonical_game_id"])
    assert game_a == game_b
    assert result_a.game_count == 1
    assert result_b.game_count == 2
    assert result_b.ply_count == 12
    assert result_b.metadata_conflict_count == 3
    assert result_b.duplicate_occurrence_count == 0
    assert _count(registry, "canonical_games") == 2
    assert _count(registry, "game_occurrences") == 4

    occurrences = registry.get_occurrences(game_a)
    occurrences_a = [
        row for row in occurrences if int(row["tournament_id"]) == tournament_a
    ]
    occurrences_b = [
        row for row in occurrences if int(row["tournament_id"]) == tournament_b
    ]
    assert len(occurrences_a) == 2
    assert len(occurrences_b) == 1
    assert registry.get_metadata_conflicts(game_a) == {
        "Event": ["Youth Open Updated", "Youth Open"],
        "Result": ["0-1", "1-0"],
        "White": ["NGUYEN, A", "Nguyen A"],
    }

    global_game = registry.get_canonical_game_by_id(game_a)
    assert global_game["canonical_occurrence_id"] == occurrences_b[0]["id"]
    global_headers = json.loads(str(global_game["canonical_headers_json"]))
    assert global_headers["Event"] == "Youth Open Updated"
    assert global_headers["White"] == "NGUYEN, A"
    assert global_headers["Result"] == "0-1"

    selected_a = revision_a_games[0]["selected_occurrence_id"]
    selected_b = revision_b_games[0]["selected_occurrence_id"]
    assert selected_a in {row["id"] for row in occurrences_a}
    assert selected_b == occurrences_b[0]["id"]
    assert selected_a != global_game["canonical_occurrence_id"]

    exported_a = _parse_games(tmp_path / "canonical-a.pgn")[0]
    exported_b = _parse_games(tmp_path / "canonical-b.pgn")[0]
    assert exported_a.headers["Event"] == "Youth Open"
    assert exported_a.headers["White"] == "Nguyen A"
    assert exported_a.headers["Result"] == "1-0"
    assert exported_b.headers["Event"] == "Youth Open Updated"
    assert exported_b.headers["White"] == "NGUYEN, A"
    assert exported_b.headers["Result"] == "0-1"


def test_truncated_prefix_gets_a_distinct_canonical_game(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("prefix-tournament")
    full_path = _copy_fixture(tmp_path, "duplicate_conflict_a.pgn", "full.pgn")
    prefix_path = _copy_fixture(tmp_path, "truncated_games.pgn", "prefix.pgn")
    full_source_file_id = _register_fixture(
        registry,
        tournament_id,
        full_path,
        source_name="full-source",
        source_priority=10,
        external_id="full",
    )
    prefix_source_file_id = _register_fixture(
        registry,
        tournament_id,
        prefix_path,
        source_name="prefix-source",
        source_priority=10,
        external_id="prefix",
    )

    full_result = canonicalize_source_file(
        registry,
        tournament_id,
        full_source_file_id,
        full_path,
        tmp_path / "full-canonical.pgn",
    )
    prefix_result = canonicalize_source_file(
        registry,
        tournament_id,
        prefix_source_file_id,
        prefix_path,
        tmp_path / "prefix-canonical.pgn",
    )

    full_game_id = _revision_game_ids(registry, full_result.revision_id)[0]
    prefix_game_id = _revision_game_ids(registry, prefix_result.revision_id)[0]
    assert full_game_id != prefix_game_id
    assert registry.get_canonical_game_by_id(full_game_id)["ply_count"] == 6
    assert registry.get_canonical_game_by_id(prefix_game_id)["ply_count"] == 3
    assert _count(registry, "canonical_games") == 2


def test_invalid_games_are_retained_as_evidence_and_excluded_from_revision(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("invalid-tournament")
    raw_path = tmp_path / "partially-invalid.pgn"
    invalid = (
        b'[Event "Invalid"]\n[Result "*"]\n\n'
        b"1. e4 e5 2. e4 *\n\n"
    )
    raw_path.write_bytes(invalid + _fixture("truncated_games.pgn"))
    source_file_id = _register_fixture(
        registry,
        tournament_id,
        raw_path,
        source_name="invalid-source",
        source_priority=10,
        external_id="invalid",
    )

    result = canonicalize_source_file(
        registry,
        tournament_id,
        source_file_id,
        raw_path,
        tmp_path / "canonical.pgn",
    )

    assert result.source_game_count == 2
    assert result.valid_game_count == 1
    assert result.invalid_game_count == 1
    occurrences = registry.get_occurrences(
        _revision_game_ids(registry, result.revision_id)[0]
    )
    assert len(occurrences) == 1
    with registry._connect() as connection:
        invalid_rows = connection.execute(
            """
            SELECT canonical_game_id, is_valid, parse_error, source_game_index
            FROM game_occurrences
            WHERE source_file_id = ? AND is_valid = 0
            """,
            (source_file_id,),
        ).fetchall()
    assert len(invalid_rows) == 1
    assert invalid_rows[0][0] is None
    assert invalid_rows[0][1] == 0
    assert invalid_rows[0][2]
    assert invalid_rows[0][3] == 1
    assert len(registry.get_revision_games(result.revision_id)) == 1


def test_zero_valid_games_fails_without_creating_revision(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("all-invalid-tournament")
    raw_path = tmp_path / "invalid.pgn"
    raw_path.write_bytes(
        b'[Event "Invalid"]\n[Result "*"]\n\n1. e4 e5 2. e4 *\n'
    )
    source_file_id = _register_fixture(
        registry,
        tournament_id,
        raw_path,
        source_name="all-invalid-source",
        source_priority=10,
        external_id="all-invalid",
    )

    with pytest.raises(RuntimeError, match="zero valid"):
        canonicalize_source_file(
            registry,
            tournament_id,
            source_file_id,
            raw_path,
            tmp_path / "canonical.pgn",
        )

    assert not (tmp_path / "canonical.pgn").exists()
    assert _count(registry, "tournament_revisions") == 0
    assert _count(registry, "game_occurrences") == 1


def test_source_provenance_must_match_registered_immutable_file(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_a = registry.upsert_tournament("provenance-a")
    tournament_b = registry.upsert_tournament("provenance-b")
    raw_path = _copy_fixture(tmp_path, "truncated_games.pgn", "raw.pgn")
    source_file_id = _register_fixture(
        registry,
        tournament_a,
        raw_path,
        source_name="provenance-source",
        source_priority=10,
        external_id="provenance",
    )

    with pytest.raises(ValueError, match="source file"):
        canonicalize_source_file(
            registry,
            tournament_a,
            99999,
            raw_path,
            tmp_path / "missing-source.pgn",
        )
    with pytest.raises(ValueError, match="tournament"):
        canonicalize_source_file(
            registry,
            tournament_b,
            source_file_id,
            raw_path,
            tmp_path / "wrong-tournament.pgn",
        )
    with pytest.raises(ValueError, match="regular file"):
        canonicalize_source_file(
            registry,
            tournament_a,
            source_file_id,
            tmp_path / "does-not-exist.pgn",
            tmp_path / "missing-raw.pgn",
        )
    with pytest.raises(ValueError, match="must differ"):
        canonicalize_source_file(
            registry,
            tournament_a,
            source_file_id,
            raw_path,
            raw_path,
        )

    original = raw_path.read_bytes()
    changed = bytes([original[0] ^ 1]) + original[1:]
    raw_path.write_bytes(changed)
    with pytest.raises(ValueError, match="SHA256"):
        canonicalize_source_file(
            registry,
            tournament_a,
            source_file_id,
            raw_path,
            tmp_path / "changed-raw.pgn",
        )
    raw_path.write_bytes(original)

    wrong_size_file = tmp_path / "wrong-size.pgn"
    wrong_size_file.write_bytes(original)
    source_id = registry.upsert_source("wrong-size-source", "https://example.invalid")
    source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_a,
        external_id="wrong-size",
    )
    wrong_size_source_file_id = registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key="raw/wrong-size/original.pgn",
        filename="wrong-size.pgn",
        sha256=hashlib.sha256(original).hexdigest(),
        byte_size=len(original) + 1,
    )
    with pytest.raises(ValueError, match="byte size"):
        canonicalize_source_file(
            registry,
            tournament_a,
            wrong_size_source_file_id,
            wrong_size_file,
            tmp_path / "wrong-size-output.pgn",
        )

    assert _count(registry, "tournament_revisions") == 0


def test_repeated_canonicalization_is_content_addressed_and_idempotent(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("idempotent-tournament")
    raw_path = _copy_fixture(tmp_path, "duplicate_conflict_a.pgn", "raw.pgn")
    source_file_id = _register_fixture(
        registry,
        tournament_id,
        raw_path,
        source_name="idempotent-source",
        source_priority=10,
        external_id="idempotent",
    )
    output_path = tmp_path / "canonical.pgn"

    first = canonicalize_source_file(
        registry, tournament_id, source_file_id, raw_path, output_path
    )
    first_bytes = output_path.read_bytes()
    first_membership = registry.get_revision_games(first.revision_id)
    first_conflicts = registry.get_metadata_conflicts(
        int(first_membership[0]["canonical_game_id"])
    )

    second = canonicalize_source_file(
        registry, tournament_id, source_file_id, raw_path, output_path
    )

    assert second == first
    assert output_path.read_bytes() == first_bytes
    assert [
        {key: row[key] for key in row if key != "id"}
        for row in registry.get_revision_games(second.revision_id)
    ] == [
        {key: row[key] for key in row if key != "id"}
        for row in first_membership
    ]
    assert registry.get_metadata_conflicts(
        int(first_membership[0]["canonical_game_id"])
    ) == first_conflicts
    assert _count(registry, "canonical_games") == 1
    assert _count(registry, "game_occurrences") == 2
    assert _count(registry, "tournament_revisions") == 1
    assert _count(registry, "tournament_games") == 1


def test_nonstandard_fen_projection_reparses_to_the_same_identity(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("fen-tournament")
    raw_path = tmp_path / "fen.pgn"
    raw_path.write_text(
        "\n".join(
            (
                '[Event "FEN fixture"]',
                '[Site "Test"]',
                '[Date "2026.09.14"]',
                '[Round "1"]',
                '[White "White"]',
                '[Black "Black"]',
                '[SetUp "1"]',
                '[FEN "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"]',
                '[Result "*"]',
                "",
                "1. Bb5 a6 *",
                "",
            )
        ),
        encoding="utf-8",
        newline="",
    )
    source_file_id = _register_fixture(
        registry,
        tournament_id,
        raw_path,
        source_name="fen-source",
        source_priority=10,
        external_id="fen",
    )
    output_path = tmp_path / "fen-canonical.pgn"

    result = canonicalize_source_file(
        registry, tournament_id, source_file_id, raw_path, output_path
    )

    original_game = _parse_games(raw_path)[0]
    exported_game = _parse_games(output_path)[0]
    assert not exported_game.errors
    assert exported_game.headers["SetUp"] == "1"
    assert exported_game.headers["FEN"].startswith(
        "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -"
    )
    assert identify_game(exported_game).fingerprint == identify_game(original_game).fingerprint
    assert result.game_count == 1
    assert result.ply_count == 2


def test_utf8_bom_is_accepted_and_removed_from_canonical_output(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("bom-tournament")
    raw_path = tmp_path / "bom.pgn"
    raw_path.write_bytes(b"\xef\xbb\xbf" + _fixture("truncated_games.pgn"))
    source_file_id = _register_fixture(
        registry,
        tournament_id,
        raw_path,
        source_name="bom-source",
        source_priority=10,
        external_id="bom",
    )

    result = canonicalize_source_file(
        registry,
        tournament_id,
        source_file_id,
        raw_path,
        tmp_path / "bom-canonical.pgn",
    )

    assert result.game_count == 1
    assert not (tmp_path / "bom-canonical.pgn").read_bytes().startswith(b"\xef\xbb\xbf")


def test_recognized_variant_is_reconstructed_without_standard_fallback(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("atomic-tournament")
    raw_path = tmp_path / "atomic.pgn"
    raw_path.write_text(
        "\n".join(
            (
                '[Event "Atomic fixture"]',
                '[Variant "Atomic"]',
                '[Result "*"]',
                "",
                "1. e4 e5 *",
                "",
            )
        ),
        encoding="utf-8",
        newline="",
    )
    source_file_id = _register_fixture(
        registry,
        tournament_id,
        raw_path,
        source_name="atomic-source",
        source_priority=10,
        external_id="atomic",
    )

    result = canonicalize_source_file(
        registry,
        tournament_id,
        source_file_id,
        raw_path,
        tmp_path / "atomic-canonical.pgn",
    )

    exported = _parse_games(tmp_path / "atomic-canonical.pgn")[0]
    assert not exported.errors
    assert exported.headers["Variant"] == "Atomic"
    assert identify_game(exported).variant == "atomic"
    assert result.game_count == 1
