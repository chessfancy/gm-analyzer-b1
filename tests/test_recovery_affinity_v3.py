import json
import sqlite3

from chessgrandmaster import production_pipeline as pp


class _Analyzer:
    def __init__(self):
        self.created = 0

    def create_run(self, scope=None):
        self.created += 1
        return 999


def test_v3_recovery_rejects_different_scheduler(tmp_path):
    db = tmp_path / "analysis.sqlite"
    pp.ensure_schema(db)
    source_sha = "scheduler-source"
    engine = {
        "workers": 2,
        "threads": 1,
        "hash_mb": 768,
        "multipv": 1,
        "depth": 19,
        "time_sec": 0.0,
        "snapshot_depths": [12, 14, 16, 18, 19],
        "scheduler": "game_affinity_lpt",
        "game_affinity": True,
        "telemetry_archive": True,
    }

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
                "pipeline_version": 3,
                "source_sha256": source_sha,
                "engine": engine,
            },
        }),),
    )
    old_run_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.commit()
    con.close()

    analyzer = _Analyzer()
    run_id, created = pp.find_or_create_run(
        analyzer,
        db,
        source_sha,
        dict(engine),
    )
    assert (run_id, created) == (old_run_id, False)

    run_id, created = pp.find_or_create_run(
        analyzer,
        db,
        source_sha,
        dict(engine, scheduler="shared_queue", game_affinity=False),
    )
    assert (run_id, created) == (999, True)
    assert analyzer.created == 1
