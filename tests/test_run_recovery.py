import json
import sqlite3

from chessgrandmaster import production_pipeline as pp


class FakeAnalyzer:
    def __init__(self):
        self.created = 0

    def create_run(self, scope=None):
        self.created += 1
        return 999


def test_depth_only_policy_bumps_pipeline_version():
    assert pp.PIPELINE_VERSION == 3


def test_changed_engine_config_does_not_recover_old_run(tmp_path):
    db = tmp_path / "analysis.sqlite"
    pp.ensure_schema(db)

    source_sha = "abc123"

    con = sqlite3.connect(db)

    con.execute(
        """
        INSERT INTO source_files(
            filename, sha256, game_count
        )
        VALUES (?, ?, ?)
        """,
        ("games.pgn", source_sha, 1),
    )

    old_scope = {
        "purpose": "production_tournament_full",
        "pipeline_version": pp.PIPELINE_VERSION,
        "source_sha256": source_sha,
        "engine": {
            "binary": "/old/path/stockfish",
            "workers": 2,
            "threads": 1,
            "hash_mb": 256,
            "multipv": 1,
            "depth": 18,
            "time_sec": 3.0,
        },
    }

    con.execute(
        """
        INSERT INTO analysis_runs(config_json)
        VALUES (?)
        """,
        (
            json.dumps({
                "scope": old_scope,
            }),
        ),
    )

    con.commit()
    con.close()

    analyzer = FakeAnalyzer()

    new_config = {
        "binary": "/new/path/stockfish",
        "workers": 2,
        "threads": 1,
        "hash_mb": 256,
        "multipv": 1,
        "depth": 20,
        "time_sec": 3.0,
    }

    run_id, created = pp.find_or_create_run(
        analyzer,
        db,
        source_sha,
        new_config,
    )

    assert created is True
    assert run_id == 999
    assert analyzer.created == 1


def test_same_engine_binary_at_different_path_recovers_run(tmp_path):
    db = tmp_path / "analysis.sqlite"
    pp.ensure_schema(db)

    source_sha = "same-source"

    old_engine = tmp_path / "deepnote" / "stockfish"
    new_engine = tmp_path / "codespaces" / "stockfish"

    old_engine.parent.mkdir()
    new_engine.parent.mkdir()

    engine_bytes = b"same-stockfish-binary"

    old_engine.write_bytes(engine_bytes)
    new_engine.write_bytes(engine_bytes)

    con = sqlite3.connect(db)

    con.execute(
        """
        INSERT INTO source_files(
            filename, sha256, game_count
        )
        VALUES (?, ?, ?)
        """,
        ("games.pgn", source_sha, 1),
    )

    old_scope = {
        "purpose": "production_tournament_full",
        "pipeline_version": pp.PIPELINE_VERSION,
        "source_sha256": source_sha,
        "engine": {
            "binary": str(old_engine),
            "workers": 2,
            "threads": 1,
            "hash_mb": 256,
            "multipv": 1,
            "depth": 18,
            "time_sec": 3.0,
        },
    }

    con.execute(
        """
        INSERT INTO analysis_runs(
            binary_sha256,
            workers,
            threads,
            hash_mb,
            multipv,
            time_limit_ms,
            depth_limit,
            config_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pp.sha256_file(old_engine),
            2,
            1,
            256,
            1,
            3000,
            18,
            json.dumps({
                "scope": old_scope,
            }),
        ),
    )

    old_run_id = con.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    con.commit()
    con.close()

    analyzer = FakeAnalyzer()

    new_config = {
        "binary": str(new_engine),
        "workers": 2,
        "threads": 1,
        "hash_mb": 256,
        "multipv": 1,
        "depth": 18,
        "time_sec": 3.0,
    }

    run_id, created = pp.find_or_create_run(
        analyzer,
        db,
        source_sha,
        new_config,
    )

    assert created is False
    assert run_id == old_run_id
    assert analyzer.created == 0


def test_legacy_depth18_time_limited_run_is_not_recovered_by_depth_only_policy(
    tmp_path,
):
    db = tmp_path / "analysis.sqlite"
    pp.ensure_schema(db)

    source_sha = "legacy-depth18-time-limited"
    con = sqlite3.connect(db)
    con.execute(
        """
        INSERT INTO source_files(filename, sha256, game_count)
        VALUES (?, ?, ?)
        """,
        ("games.pgn", source_sha, 1),
    )
    con.execute(
        """
        INSERT INTO analysis_runs(
            workers, threads, hash_mb, multipv,
            time_limit_ms, depth_limit, config_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            2,
            1,
            256,
            1,
            3000,
            18,
            json.dumps(
                {
                    "scope": {
                        "purpose": "production_tournament_full",
                        "pipeline_version": 1,
                        "source_sha256": source_sha,
                        "engine": {
                            "workers": 2,
                            "threads": 1,
                            "hash_mb": 256,
                            "multipv": 1,
                            "depth": 18,
                            "time_sec": 3.0,
                        },
                    },
                }
            ),
        ),
    )
    con.commit()
    con.close()

    analyzer = FakeAnalyzer()
    run_id, created = pp.find_or_create_run(
        analyzer,
        db,
        source_sha,
        {
            "workers": 2,
            "threads": 1,
            "hash_mb": 256,
            "multipv": 1,
            "depth": 18,
            "time_sec": 0.0,
            "snapshot_depths": [12, 14, 16, 18, 19],
        },
    )

    assert created is True
    assert run_id == 999


def test_v2_run_is_not_recovered_by_v3_even_when_engine_values_match(tmp_path):
    db = tmp_path / "analysis.sqlite"
    pp.ensure_schema(db)
    source_sha = "v2-source"

    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO source_files(filename, sha256, game_count) VALUES (?, ?, ?)",
        ("games.pgn", source_sha, 1),
    )
    con.execute(
        "INSERT INTO analysis_runs(config_json) VALUES (?)",
        (json.dumps({
            "scope": {
                "purpose": "production_tournament_full",
                "pipeline_version": 2,
                "source_sha256": source_sha,
                "engine": {
                    "workers": 2,
                    "threads": 1,
                    "hash_mb": 256,
                    "multipv": 1,
                    "depth": 18,
                    "time_sec": 0.0,
                    "snapshot_depths": [12, 14, 16, 18, 19],
                    "scheduler": "game_affinity_lpt",
                },
            },
        }),),
    )
    con.commit()
    con.close()

    analyzer = FakeAnalyzer()
    run_id, created = pp.find_or_create_run(
        analyzer,
        db,
        source_sha,
        {
            "workers": 2,
            "threads": 1,
            "hash_mb": 256,
            "multipv": 1,
            "depth": 18,
            "time_sec": 0.0,
            "snapshot_depths": [12, 14, 16, 18, 19],
            "scheduler": "game_affinity_lpt",
        },
    )

    assert created is True
    assert run_id == 999
    assert analyzer.created == 1


def test_v3_recovery_requires_matching_affinity_and_hash(tmp_path):
    db = tmp_path / "analysis.sqlite"
    pp.ensure_schema(db)
    source_sha = "v3-source"
    scope = {
        "purpose": "production_tournament_full",
        "pipeline_version": 3,
        "source_sha256": source_sha,
        "engine": {
            "workers": 2,
            "threads": 1,
            "hash_mb": 768,
            "multipv": 1,
            "depth": 19,
            "time_sec": 0.0,
            "snapshot_depths": [12, 14, 16, 18, 19],
            "scheduler": "game_affinity_lpt",
        },
    }
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO source_files(filename, sha256, game_count) VALUES (?, ?, ?)",
        ("games.pgn", source_sha, 1),
    )
    con.execute(
        "INSERT INTO analysis_runs(config_json) VALUES (?)",
        (json.dumps({"scope": scope}),),
    )
    old_run_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()
    con.close()

    analyzer = FakeAnalyzer()
    matching = dict(scope["engine"])
    run_id, created = pp.find_or_create_run(
        analyzer,
        db,
        source_sha,
        matching,
    )
    assert (run_id, created) == (old_run_id, False)

    different_hash = dict(matching, hash_mb=1024)
    run_id, created = pp.find_or_create_run(
        analyzer,
        db,
        source_sha,
        different_hash,
    )
    assert created is True
    assert run_id == 999
    assert analyzer.created == 1
