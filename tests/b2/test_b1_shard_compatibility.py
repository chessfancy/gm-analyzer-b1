import hashlib
import sqlite3
from pathlib import Path

from chessgrandmaster.b2.canonicalize import canonicalize_source_file
from chessgrandmaster.b2.packaging import PackageService
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.b2.storage import LocalObjectStore
from chessgrandmaster.production_pipeline import import_pgn


PGN = """[Event \"Compatibility One\"]
[Site \"Test\"]
[Date \"2026.09.21\"]
[Round \"1\"]
[White \"White One\"]
[Black \"Black One\"]
[Result \"*\"]

1. e4 e5 2. Nf3 Nc6 *

[Event \"Compatibility Two\"]
[Site \"Test\"]
[Date \"2026.09.21\"]
[Round \"2\"]
[White \"White Two\"]
[Black \"Black Two\"]
[Result \"*\"]

1. d4 d5 2. c4 e6 *
"""


def packaged_fixture(tmp_path: Path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament(
        "compatibility-fixture",
        name="Compatibility Fixture",
        status="CANONICALIZED",
    )
    source_id = registry.upsert_source("fixture", "https://example.invalid")
    source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id="compatibility-fixture",
    )
    source_path = tmp_path / "input.pgn"
    source_path.write_text(PGN, encoding="utf-8", newline="")
    source_bytes = source_path.read_bytes()
    source_file_id = registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key="raw/compatibility/input.pgn",
        filename=source_path.name,
        sha256=hashlib.sha256(source_bytes).hexdigest(),
        byte_size=len(source_bytes),
    )
    canonicalize_source_file(
        registry,
        tournament_id,
        source_file_id,
        source_path,
        tmp_path / "canonical.pgn",
    )
    store = LocalObjectStore(tmp_path / "objects")
    result = PackageService(registry, store, tmp_path / "workspace").package(
        "compatibility-fixture",
        target_plies=3000,
    )
    shard = result.shards[0]
    shard_path = tmp_path / "shard-input.pgn"
    store.get_file(shard.input_key, shard_path)
    return result, shard, shard_path


def test_packaged_shard_imports_into_b1_without_semantic_adapter(tmp_path):
    result, shard, shard_path = packaged_fixture(tmp_path)
    imported = import_pgn(shard_path, tmp_path / "analysis.sqlite")

    assert imported["games"] == shard.games
    assert imported["moves"] == shard.plies
    assert imported["games"] == result.game_count
    with sqlite3.connect(tmp_path / "analysis.sqlite") as connection:
        rows = connection.execute(
            "SELECT source_game_index FROM games ORDER BY source_game_index"
        ).fetchall()
    assert [row[0] for row in rows] == list(range(1, shard.games + 1))
