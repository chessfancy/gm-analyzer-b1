import sqlite3

from chessgrandmaster.analyzer import TournamentAnalyzer
from chessgrandmaster.production_pipeline import ensure_schema


def _seed_move_and_run(db_path):
    con = sqlite3.connect(db_path)
    source_id = con.execute(
        "INSERT INTO source_files(filename, sha256) VALUES (?, ?)",
        ("game.pgn", "source-sha"),
    ).lastrowid
    game_id = con.execute(
        """
        INSERT INTO games(
            source_file_id, source_game_index, initial_fen, headers_json
        ) VALUES (?, ?, ?, ?)
        """,
        (source_id, 1, "start-fen", "{}"),
    ).lastrowid
    move_id = con.execute(
        """
        INSERT INTO moves(
            game_id, ply, uci, san, fen_before, fen_after
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_id, 1, "e2e4", "e4", "start-fen", "after-fen"),
    ).lastrowid
    run_id = con.execute(
        "INSERT INTO analysis_runs(config_json) VALUES (?)",
        ("{}",),
    ).lastrowid
    con.commit()
    con.close()
    return run_id, move_id


def _result(move_id, *, started_at, finished_at, cp_offset=0):
    response = {
        "rank": 0,
        "is_played": True,
        "source_search": "primary",
        "uci": "e2e4",
        "cp": 30 + cp_offset,
        "mate": 0,
        "depth": 14,
        "seldepth": 18,
        "nodes": 2400,
        "nps": 12000,
        "time_ms": 200,
        "pv_uci": "e2e4 e7e5",
    }
    snapshots = [
        {
            "source_search": "primary",
            "checkpoint_depth": checkpoint,
            "reported_depth": checkpoint,
            "uci": "e2e4",
            "cp": cp + cp_offset,
            "mate": 0,
            "wdl_wins": 400,
            "wdl_draws": 450,
            "wdl_losses": 150,
            "seldepth": checkpoint + 3,
            "nodes": checkpoint * 100,
            "nps": 12000,
            "time_ms": checkpoint * 10,
            "pv_uci": "e2e4 e7e5",
        }
        for checkpoint, cp in ((12, 20), (14, 30))
    ]
    return {
        "ok": True,
        "move_id": move_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "played_rank": 0,
        "lucas_loss": 0.0,
        "category": "Best move",
        "nag": 0,
        "second_search": False,
        "responses": [response],
        "depth_snapshots": snapshots,
    }


def test_write_results_persists_timestamps_and_replaces_depth_snapshots(tmp_path):
    db_path = tmp_path / "analysis.sqlite"
    ensure_schema(db_path)
    run_id, move_id = _seed_move_and_run(db_path)
    analyzer = TournamentAnalyzer(db_path, tmp_path / "stockfish")

    first = _result(
        move_id,
        started_at="2026-09-13T01:02:03+00:00",
        finished_at="2026-09-13T01:02:05+00:00",
    )
    assert analyzer._write_results(run_id, [first]) == (1, 0)

    con = sqlite3.connect(db_path)
    analysis_id, started_at, finished_at = con.execute(
        """
        SELECT id, started_at, finished_at
        FROM move_analysis
        WHERE run_id=? AND move_id=?
        """,
        (run_id, move_id),
    ).fetchone()
    snapshots = con.execute(
        """
        SELECT checkpoint_depth, reported_depth, cp, wdl_wins,
               wdl_draws, wdl_losses
        FROM engine_depth_snapshots
        WHERE analysis_id=?
        ORDER BY checkpoint_depth
        """,
        (analysis_id,),
    ).fetchall()
    con.close()

    assert started_at == "2026-09-13T01:02:03+00:00"
    assert finished_at == "2026-09-13T01:02:05+00:00"
    assert snapshots == [
        (12, 12, 20, 400, 450, 150),
        (14, 14, 30, 400, 450, 150),
    ]

    retry = _result(
        move_id,
        started_at="2026-09-13T02:00:00+00:00",
        finished_at="2026-09-13T02:00:04+00:00",
        cp_offset=100,
    )
    assert analyzer._write_results(run_id, [retry]) == (1, 0)

    con = sqlite3.connect(db_path)
    timestamps = con.execute(
        "SELECT started_at, finished_at FROM move_analysis WHERE id=?",
        (analysis_id,),
    ).fetchone()
    replacement_rows = con.execute(
        """
        SELECT checkpoint_depth, cp
        FROM engine_depth_snapshots
        WHERE analysis_id=?
        ORDER BY checkpoint_depth
        """,
        (analysis_id,),
    ).fetchall()
    con.close()

    assert timestamps == (
        "2026-09-13T02:00:00+00:00",
        "2026-09-13T02:00:04+00:00",
    )
    assert replacement_rows == [(12, 120), (14, 130)]
