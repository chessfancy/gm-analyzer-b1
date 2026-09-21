import hashlib
import json
from pathlib import Path

import pytest

from chessgrandmaster.b2.canonicalize import canonicalize_source_file
from chessgrandmaster.b2.packaging import PackageService
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.b2.storage import LocalObjectStore


PGN = """[Event \"Fixture One\"]
[Site \"Test\"]
[Date \"2026.09.21\"]
[Round \"1\"]
[White \"White One\"]
[Black \"Black One\"]
[Result \"1/2-1/2\"]

1. e4 e5 2. Nf3 Nc6 1/2-1/2

[Event \"Fixture Two\"]
[Site \"Test\"]
[Date \"2026.09.21\"]
[Round \"2\"]
[White \"White Two\"]
[Black \"Black Two\"]
[Result \"1-0\"]

1. d4 d5 2. c4 e6 1-0

[Event \"Fixture Three\"]
[Site \"Test\"]
[Date \"2026.09.21\"]
[Round \"3\"]
[White \"White Three\"]
[Black \"Black Three\"]
[Result \"0-1\"]

1. Nf3 d5 2. g3 c5 0-1

[Event \"Fixture Four\"]
[Site \"Test\"]
[Date \"2026.09.21\"]
[Round \"4\"]
[White \"White Four\"]
[Black \"Black Four\"]
[Result \"*\"]

1. c4 e5 2. Nc3 Nf6 *
"""


def seed_packaging_fixture(tmp_path: Path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament(
        "fixture-tournament",
        name="Fixture Tournament",
        site="https://example.invalid/fixture",
        time_control_class="classical",
        is_otb=True,
        has_vietnamese_player=False,
        priority_score=10,
        priority_reasons=["fixture"],
        status="CANONICALIZED",
    )
    source_id = registry.upsert_source("fixture", "https://example.invalid", priority=10)
    source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id="fixture-1",
        source_url="https://example.invalid/fixture-1",
        pgn_url="https://example.invalid/fixture-1.pgn",
    )
    raw_path = tmp_path / "fixture.pgn"
    raw_path.write_text(PGN, encoding="utf-8", newline="")
    raw_bytes = raw_path.read_bytes()
    source_file_id = registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key="raw/fixture/original.pgn",
        filename=raw_path.name,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        byte_size=len(raw_bytes),
        content_type="application/x-chess-pgn",
    )
    canonical_path = tmp_path / "canonical.pgn"
    canonical = canonicalize_source_file(
        registry,
        tournament_id,
        source_file_id,
        raw_path,
        canonical_path,
    )
    return registry, tournament_id, canonical


def test_registry_packaging_dto_has_ordered_games_and_provenance(tmp_path):
    registry, tournament_id, canonical = seed_packaging_fixture(tmp_path)

    source = registry.get_revision_for_packaging("fixture-tournament", canonical.revision_number)

    assert source.tournament_id == "fixture-tournament"
    assert source.revision_number == 1
    assert source.canonical_sha256 == canonical.canonical_sha256
    assert [game.ordinal for game in source.games] == [1, 2, 3, 4]
    assert [game.ply_count for game in source.games] == [4, 4, 4, 4]
    assert source.sources[0].provider == "fixture"
    assert source.sources[0].external_id == "fixture-1"
    assert source.games[0].selected_headers["Event"] == "Fixture One"
    assert source.registry_tournament_id == tournament_id


def test_package_service_emits_interoperable_objects_and_is_idempotent(tmp_path):
    registry, tournament_id, canonical = seed_packaging_fixture(tmp_path)
    store = LocalObjectStore(tmp_path / "objects")
    service = PackageService(registry, store, tmp_path / "workspace")

    first = service.package("fixture-tournament", target_plies=6)
    first_bytes = {key: (tmp_path / "objects" / key).read_bytes() for key in store.list("")}
    second = service.package("fixture-tournament", target_plies=6)
    second_bytes = {key: (tmp_path / "objects" / key).read_bytes() for key in store.list("")}

    assert first.to_summary() == second.to_summary()
    assert first_bytes == second_bytes
    assert registry.get_tournament_status(tournament_id) == "READY"
    assert first.shard_count == 3
    assert first.game_count == 4
    assert first.ply_count == 16

    expected_prefix = "tournaments/fixture-tournament/revisions/0001"
    keys = store.list(expected_prefix)
    assert f"{expected_prefix}/canonical/tournament.pgn" in keys
    assert f"{expected_prefix}/canonical/manifest.json" in keys
    assert all(key.endswith(('.pgn', '.json')) for key in keys)
    for shard in first.shards:
        assert store.exists(shard.input_key)
        assert store.exists(shard.job_key)
        assert store.exists(shard.manifest_key)
        assert store.exists(shard.checksums_key)
        job = json.loads((tmp_path / "objects" / shard.job_key).read_text(encoding="utf-8"))
        assert job["job_id"] == shard.job_id
        assert job["input"]["sha256"] == store.stat(shard.input_key).sha256
        assert job["tournament_id"] == "fixture-tournament"
        assert job["tournament_revision"] == 1
        assert job["shard_index"] == shard.shard_index
        assert len(job["input"]["canonical_game_fingerprints"]) == job["input"]["games"]
        assert "provider" not in json.dumps(job)
        assert "workers" not in json.dumps(job)
        assert "threads" not in json.dumps(job)
        assert "Hash" not in json.dumps(job)

    canonical_object = tmp_path / "objects" / first.canonical_pgn_key
    assert canonical_object.read_bytes() == canonical.canonical_path.read_bytes()


def test_package_service_rejects_checksum_mismatch_before_ready(tmp_path):
    registry, tournament_id, canonical = seed_packaging_fixture(tmp_path)
    with registry._connect() as connection:
        connection.execute(
            "UPDATE tournament_revisions SET canonical_sha256 = ? WHERE id = ?",
            ("0" * 64, canonical.revision_id),
        )
    service = PackageService(
        registry,
        LocalObjectStore(tmp_path / "objects"),
        tmp_path / "workspace",
    )

    with pytest.raises(ValueError, match="canonical"):
        service.package("fixture-tournament")
    assert registry.get_tournament_status(tournament_id) == "CANONICALIZED"
