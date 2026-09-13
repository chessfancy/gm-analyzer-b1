import json
import sqlite3

from chessgrandmaster.analyzer import TournamentAnalyzer
from chessgrandmaster.production_pipeline import ensure_schema


def _seed_new_db(db_path):
    ensure_schema(db_path)
    con = sqlite3.connect(db_path)
    source_id = con.execute(
        "INSERT INTO source_files(filename, sha256) VALUES (?, ?)",
        ("game.pgn", "source"),
    ).lastrowid
    game_id = con.execute(
        """
        INSERT INTO games(
            source_file_id, source_game_index, initial_fen, headers_json
        ) VALUES (?, ?, ?, ?)
        """,
        (source_id, 1, "start", "{}"),
    ).lastrowid
    move_id = con.execute(
        """
        INSERT INTO moves(
            game_id, ply, uci, san, fen_before, fen_after
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_id, 1, "e2e4", "e4", "start", "after"),
    ).lastrowid
    run_id = con.execute(
        "INSERT INTO analysis_runs(config_json) VALUES (?)", ("{}",)
    ).lastrowid
    con.commit()
    con.close()
    return run_id, move_id


def _result(move_id, execution_id, worker_id, session_id, cp=30):
    info_json = json.dumps({"depth": 14, "hashfull": 321})
    return {
        "type": "job_result",
        "ok": True,
        "execution_id": execution_id,
        "worker_id": worker_id,
        "engine_session_id": session_id,
        "move_id": move_id,
        "started_at": "2026-09-13T01:00:00+00:00",
        "finished_at": "2026-09-13T01:00:01+00:00",
        "played_rank": 0,
        "lucas_loss": 0.0,
        "category": "Best move",
        "nag": 0,
        "second_search": False,
        "responses": [{
            "rank": 0,
            "is_played": True,
            "source_search": "primary",
            "uci": "e2e4",
            "cp": cp,
            "mate": 0,
            "depth": 14,
            "seldepth": 16,
            "nodes": 100,
            "nps": 1000,
            "time_ms": 100,
            "pv_uci": "e2e4",
            "hashfull": 321,
            "tbhits": 7,
            "info_json": info_json,
            "execution_id": execution_id,
            "worker_id": worker_id,
            "engine_session_id": session_id,
        }],
        "depth_snapshots": [{
            "source_search": "primary",
            "checkpoint_depth": 12,
            "reported_depth": 12,
            "uci": "e2e4",
            "cp": cp,
            "mate": 0,
            "wdl_wins": 400,
            "wdl_draws": 400,
            "wdl_losses": 200,
            "seldepth": 14,
            "nodes": 80,
            "nps": 800,
            "time_ms": 80,
            "pv_uci": "e2e4",
            "hashfull": 321,
            "tbhits": 7,
            "info_json": info_json,
            "execution_id": execution_id,
            "worker_id": worker_id,
            "engine_session_id": session_id,
        }],
    }


def test_write_results_persists_telemetry_and_retry_provenance(tmp_path):
    db_path = tmp_path / "analysis.sqlite"
    run_id, move_id = _seed_new_db(db_path)
    analyzer = TournamentAnalyzer(db_path, tmp_path / "stockfish")

    first = _result(move_id, "exec-1", 0, "session-1")
    assert analyzer._write_results(run_id, [first]) == (1, 0)

    retry = _result(move_id, "exec-2", 1, "session-2", cp=40)
    assert analyzer._write_results(run_id, [retry]) == (1, 0)

    con = sqlite3.connect(db_path)
    move_row = con.execute(
        """
        SELECT execution_id, worker_id, engine_session_id
        FROM move_analysis
        WHERE run_id=? AND move_id=?
        """,
        (run_id, move_id),
    ).fetchone()
    response_row = con.execute(
        """
        SELECT cp, hashfull, tbhits, info_json,
               execution_id, worker_id, engine_session_id
        FROM engine_responses
        """
    ).fetchone()
    snapshot_row = con.execute(
        """
        SELECT cp, hashfull, tbhits, info_json,
               execution_id, worker_id, engine_session_id
        FROM engine_depth_snapshots
        """
    ).fetchone()
    con.close()

    assert move_row == ("exec-2", 1, "session-2")
    assert response_row[:3] == (40, 321, 7)
    assert json.loads(response_row[3])["hashfull"] == 321
    assert response_row[4:] == ("exec-2", 1, "session-2")
    assert snapshot_row[:3] == (40, 321, 7)
    assert json.loads(snapshot_row[3])["hashfull"] == 321
    assert snapshot_row[4:] == ("exec-2", 1, "session-2")


def test_ensure_schema_migrates_old_v2_tables_without_data_loss(tmp_path):
    db_path = tmp_path / "old.sqlite"
    con = sqlite3.connect(db_path)
    con.executescript("""
        CREATE TABLE source_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE,
            game_count INTEGER DEFAULT 0
        );
        CREATE TABLE games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file_id INTEGER NOT NULL,
            source_game_index INTEGER NOT NULL,
            initial_fen TEXT NOT NULL,
            headers_json TEXT NOT NULL
        );
        CREATE TABLE moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            ply INTEGER NOT NULL,
            uci TEXT NOT NULL,
            san TEXT NOT NULL,
            fen_before TEXT NOT NULL,
            fen_after TEXT NOT NULL
        );
        CREATE TABLE analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_json TEXT
        );
        CREATE TABLE move_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            move_id INTEGER NOT NULL,
            status TEXT,
            category TEXT
        );
        CREATE TABLE engine_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER NOT NULL,
            rank INTEGER,
            source_search TEXT,
            uci TEXT,
            cp INTEGER,
            mate INTEGER,
            depth INTEGER,
            seldepth INTEGER,
            nodes INTEGER,
            nps INTEGER,
            time_ms INTEGER,
            pv_uci TEXT
        );
        INSERT INTO source_files(filename, sha256) VALUES ('old.pgn', 'old');
    """)
    con.commit()
    con.close()

    ensure_schema(db_path)
    ensure_schema(db_path)

    con = sqlite3.connect(db_path)
    source_row = con.execute(
        "SELECT filename, sha256 FROM source_files"
    ).fetchone()
    columns = {
        row[1]
        for row in con.execute("PRAGMA table_info(move_analysis)")
    }
    response_columns = {
        row[1]
        for row in con.execute("PRAGMA table_info(engine_responses)")
    }
    snapshot_columns = {
        row[1]
        for row in con.execute(
            "PRAGMA table_info(engine_depth_snapshots)"
        )
    }
    archive_table = con.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type='table' AND name='uci_event_archives'
        """
    ).fetchone()
    con.close()

    assert source_row == ("old.pgn", "old")
    assert {"execution_id", "worker_id", "engine_session_id"} <= columns
    assert {
        "hashfull", "tbhits", "info_json",
        "execution_id", "worker_id", "engine_session_id",
    } <= response_columns
    assert {
        "hashfull", "tbhits", "info_json",
        "execution_id", "worker_id", "engine_session_id",
    } <= snapshot_columns
    assert archive_table is not None
