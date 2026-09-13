import sqlite3

from chessgrandmaster.production_pipeline import ensure_schema, execution_audit


def _minimal_execution(tmp_path):
    db_path = tmp_path / "analysis.sqlite"
    ensure_schema(db_path)
    con = sqlite3.connect(db_path)
    source_id = con.execute(
        "INSERT INTO source_files(filename, sha256) VALUES (?, ?)",
        ("games.pgn", "source"),
    ).lastrowid
    game_id = con.execute(
        """
        INSERT INTO games(source_file_id, source_game_index, initial_fen, headers_json)
        VALUES (?, ?, ?, ?)
        """,
        (source_id, 1, "start", "{}"),
    ).lastrowid
    move_id = con.execute(
        """
        INSERT INTO moves(game_id, ply, uci, san, fen_before, fen_after)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (game_id, 1, "e2e4", "e4", "start", "after"),
    ).lastrowid
    run_id = con.execute(
        "INSERT INTO analysis_runs(config_json) VALUES (?)", ("{}",)
    ).lastrowid
    analysis_id = con.execute(
        """
        INSERT INTO move_analysis(
            run_id, move_id, status, execution_id, worker_id, engine_session_id
        ) VALUES (?, ?, 'completed', ?, ?, ?)
        """,
        (run_id, move_id, "exec-current", 0, "session-0"),
    ).lastrowid
    con.commit()
    con.close()
    return db_path, run_id, analysis_id


def test_execution_audit_reports_current_provenance_and_archives(tmp_path):
    db_path = tmp_path / "analysis.sqlite"
    ensure_schema(db_path)
    con = sqlite3.connect(db_path)
    source_id = con.execute(
        "INSERT INTO source_files(filename, sha256) VALUES (?, ?)",
        ("games.pgn", "source"),
    ).lastrowid
    game_one = con.execute(
        """
        INSERT INTO games(source_file_id, source_game_index, initial_fen, headers_json)
        VALUES (?, ?, ?, ?)
        """,
        (source_id, 1, "start", "{}"),
    ).lastrowid
    game_two = con.execute(
        """
        INSERT INTO games(source_file_id, source_game_index, initial_fen, headers_json)
        VALUES (?, ?, ?, ?)
        """,
        (source_id, 2, "start", "{}"),
    ).lastrowid
    move_ids = []
    for game_id, ply in ((game_one, 1), (game_one, 2), (game_two, 1)):
        move_ids.append(con.execute(
            """
            INSERT INTO moves(game_id, ply, uci, san, fen_before, fen_after)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (game_id, ply, "e2e4", "e4", "start", "after"),
        ).lastrowid)
    run_id = con.execute(
        "INSERT INTO analysis_runs(config_json) VALUES (?)", ("{}",)
    ).lastrowid
    con.commit()

    # Game one has a deliberate current-execution affinity violation.
    for move_id, worker_id, session_id in (
        (move_ids[0], 0, "session-0"),
        (move_ids[1], 1, "session-1"),
        (move_ids[2], 1, "session-1"),
    ):
        analysis_id = con.execute(
            """
            INSERT INTO move_analysis(
                run_id, move_id, status, execution_id, worker_id, engine_session_id
            ) VALUES (?, ?, 'completed', ?, ?, ?)
            """,
            (run_id, move_id, "exec-current", worker_id, session_id),
        ).lastrowid
        con.execute(
            """
            INSERT INTO engine_depth_snapshots(
                analysis_id, source_search, checkpoint_depth, reported_depth,
                cp, mate, hashfull, tbhits, info_json,
                execution_id, worker_id, engine_session_id
            ) VALUES (?, 'primary', 12, 12, 20, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis_id,
                100 if move_id != move_ids[2] else None,
                3 if move_id == move_ids[0] else None,
                '{"depth":12}' if move_id != move_ids[2] else None,
                "exec-current",
                worker_id,
                session_id,
            ),
        )
    # A historical execution is not included in current-execution counts.
    con.execute(
        """
        INSERT INTO uci_event_archives(
            run_id, execution_id, worker_id, engine_session_id,
            relative_path, compression, event_count, created_at, closed_at
        ) VALUES (?, ?, ?, ?, ?, 'gzip', ?, ?, ?)
        """,
        (run_id, "exec-current", 0, "session-0", "uci/a.jsonl.gz", 4, "a", "b"),
    )
    con.execute(
        """
        INSERT INTO uci_event_archives(
            run_id, execution_id, worker_id, engine_session_id,
            relative_path, compression, event_count, created_at, closed_at
        ) VALUES (?, ?, ?, ?, ?, 'gzip', ?, ?, ?)
        """,
        (run_id, "exec-old", 9, "old-session", "uci/old.jsonl.gz", 99, "a", "b"),
    )
    con.commit()
    con.close()

    audit = execution_audit(db_path, run_id, "exec-current")

    assert audit["execution_id"] == "exec-current"
    assert audit["workers_used"] == 2
    assert audit["engine_sessions"] == 2
    assert audit["analyzed_games"] == 2
    assert audit["affinity_violations"] == 1
    assert audit["uci_archive_files"] == 1
    assert audit["archived_info_event_count"] == 4
    assert audit["snapshots_info_json_missing"] == 1
    assert audit["snapshots_hashfull_available"] == 2
    assert audit["snapshots_tbhits_available"] == 1


def test_execution_audit_reports_missing_response_info_json(tmp_path):
    db_path, run_id, analysis_id = _minimal_execution(tmp_path)
    con = sqlite3.connect(db_path)
    con.execute(
        """
        INSERT INTO engine_responses(
            analysis_id, rank, source_search, depth, info_json
        ) VALUES (?, ?, 'primary', ?, ?)
        """,
        (analysis_id, 1, 19, None),
    )
    con.execute(
        """
        INSERT INTO engine_responses(
            analysis_id, rank, source_search, depth, info_json
        ) VALUES (?, ?, 'post_move', ?, ?)
        """,
        (analysis_id, 0, 19, "{\"depth\":19}"),
    )
    con.commit()
    con.close()

    audit = execution_audit(db_path, run_id, "exec-current")

    assert audit["engine_responses_info_json_missing"] == 1
    assert audit["final_response_info_json_missing"] == 1


def test_execution_audit_reports_unclosed_archive(tmp_path):
    db_path, run_id, _ = _minimal_execution(tmp_path)
    con = sqlite3.connect(db_path)
    con.execute(
        """
        INSERT INTO uci_event_archives(
            run_id, execution_id, worker_id, engine_session_id,
            relative_path, compression, event_count, created_at, closed_at
        ) VALUES (?, ?, ?, ?, ?, 'gzip', ?, ?, NULL)
        """,
        (run_id, "exec-current", 0, "session-0", "uci/a.jsonl.gz", 4, "a"),
    )
    con.commit()
    con.close()

    audit = execution_audit(
        db_path,
        run_id,
        "exec-current",
        archive_root=tmp_path / "archives",
    )

    assert audit["uci_archive_files"] == 1
    assert audit["uci_archives_not_closed"] == 1
    assert audit["uci_archives_not_persisted"] == 1
