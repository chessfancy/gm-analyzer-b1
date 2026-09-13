import sqlite3

import pytest

from chessgrandmaster import production_pipeline
from chessgrandmaster.production_pipeline import snapshot_audit


ensure_schema = production_pipeline.ensure_schema


EXPECTED_DEPTHS = (12, 14, 16, 18, 19)


def _fixture(
    tmp_path,
    *,
    missing_primary=(),
    missing_post=(),
    final_mismatch=False,
    final_depth=19,
    snapshot_depths=EXPECTED_DEPTHS,
):
    db_path = tmp_path / "analysis.sqlite"
    ensure_schema(db_path)

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
        "INSERT INTO analysis_runs(config_json) VALUES (?)", ("{}",)
    ).lastrowid
    analysis_id = con.execute(
        """
        INSERT INTO move_analysis(run_id, move_id, status)
        VALUES (?, ?, 'completed')
        """,
        (run_id, move_id),
    ).lastrowid

    for depth in snapshot_depths:
        if depth not in missing_primary:
            con.execute(
                """
                INSERT INTO engine_depth_snapshots(
                    analysis_id, source_search, checkpoint_depth,
                    reported_depth, uci, cp, mate, pv_uci
                ) VALUES (?, 'primary', ?, ?, ?, ?, ?, ?)
                """,
                (analysis_id, depth, depth, "e2e4", 25, 0, "e2e4 e7e5"),
            )

    con.execute(
        """
        INSERT INTO engine_responses(
            analysis_id, rank, source_search, uci, cp, mate, depth, pv_uci
        ) VALUES (?, 0, 'primary', ?, ?, ?, ?, ?)
        """,
        (
            analysis_id,
            "e2e4",
            25 if not final_mismatch else 30,
            0,
            final_depth,
            "e2e4 e7e5",
        ),
    )

    if missing_post is not None:
        for depth in snapshot_depths:
            if depth not in missing_post:
                con.execute(
                    """
                    INSERT INTO engine_depth_snapshots(
                        analysis_id, source_search, checkpoint_depth,
                        reported_depth, uci, cp, mate, pv_uci
                    ) VALUES (?, 'post_move', ?, ?, ?, ?, ?, ?)
                    """,
                    (analysis_id, depth, depth, "e2e4", 10, 0, "e2e4 e7e5"),
                )
        con.execute(
            """
            INSERT INTO engine_responses(
                analysis_id, rank, source_search, uci, cp, mate, depth, pv_uci
            ) VALUES (?, 0, 'post_move', 'e2e4', 10, 0, ?, 'e2e4 e7e5')
            """,
            (analysis_id, final_depth),
        )

    con.commit()
    con.close()
    return db_path, run_id


def test_snapshot_audit_complete_primary_set_has_zero_missing(tmp_path, capsys):
    db_path, run_id = _fixture(tmp_path)

    audit = snapshot_audit(db_path, run_id, EXPECTED_DEPTHS)

    assert audit["primary_missing"] == 0
    assert audit["post_move_missing"] == 0
    assert audit["final_mismatches"] == 0
    assert "DEPTH SNAPSHOT AUDIT" in capsys.readouterr().out


def test_snapshot_audit_reports_one_missing_post_move_checkpoint(tmp_path):
    db_path, run_id = _fixture(tmp_path, missing_post={16})

    audit = snapshot_audit(db_path, run_id, EXPECTED_DEPTHS)

    assert audit["primary_missing"] == 0
    assert audit["post_move_missing"] == 1


def test_snapshot_audit_reports_missing_primary_checkpoint(tmp_path):
    db_path, run_id = _fixture(tmp_path, missing_primary={14})

    audit = snapshot_audit(db_path, run_id, EXPECTED_DEPTHS)

    assert audit["primary_missing"] == 1


def test_snapshot_audit_reports_final_primary_mismatch(tmp_path):
    db_path, run_id = _fixture(tmp_path, final_mismatch=True)

    audit = snapshot_audit(db_path, run_id, EXPECTED_DEPTHS)

    assert audit["final_mismatches"] == 1


def test_snapshot_audit_low_final_depth_without_active_checkpoint_passes(tmp_path):
    db_path, run_id = _fixture(
        tmp_path,
        missing_post=None,
        final_depth=8,
        snapshot_depths=(),
    )

    audit = snapshot_audit(db_path, run_id, ())

    assert audit["primary_missing"] == 0
    assert audit["post_move_missing"] == 0
    assert audit["final_snapshot_missing"] == 0
    assert audit["final_mismatches"] == 0


def test_run_pipeline_rejects_incomplete_snapshot_audit(tmp_path, monkeypatch):
    pgn_path = tmp_path / "input.pgn"
    pgn_path.write_text(
        '[Event "Synthetic"]\n\n1. e4 *\n',
        encoding="utf-8",
    )

    class FakeAnalyzer:
        def __init__(self, *args, **kwargs):
            self._statuses = iter((
                {"total": 1, "completed": 0, "failed": 0, "pending": 1},
                {"total": 1, "completed": 1, "failed": 0, "pending": 0},
            ))

        def status(self, run_id):
            return next(self._statuses)

        def analyze(self, run_id):
            return {"completed": 1, "failed": 0}

    monkeypatch.setattr(production_pipeline, "TournamentAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(
        production_pipeline,
        "resolve_engine_binary",
        lambda value=None: tmp_path / "stockfish",
    )
    monkeypatch.setattr(
        production_pipeline,
        "verify_engine_binary",
        lambda path: {"label": "synthetic", "output_tag": "synthetic"},
    )
    monkeypatch.setattr(
        production_pipeline,
        "find_or_create_run",
        lambda *args, **kwargs: (1, True),
    )
    monkeypatch.setattr(
        production_pipeline,
        "resource_summary",
        lambda *args, **kwargs: {
            "sample_count": 0,
            "configured_hash_total_bytes": 0,
            "peak_stockfish_rss_bytes": 0,
            "peak_cgm_process_tree_rss_bytes": 0,
            "peak_cgroup_memory_current_bytes": 0,
            "cgroup_memory_max_bytes": 0,
            "minimum_mem_available_bytes": 0,
            "max_stockfish_process_count": 0,
        },
    )
    monkeypatch.setattr(
        production_pipeline,
        "database_audit",
        lambda *args, **kwargs: {
            "completed": 1,
            "responses": 1,
            "second_searches": 0,
            "bad_nags": 0,
            "categories": {},
        },
    )
    monkeypatch.setattr(
        production_pipeline,
        "snapshot_audit",
        lambda *args, **kwargs: {
            "primary_missing": 1,
            "post_move_missing": 0,
            "final_mismatches": 0,
        },
    )

    with pytest.raises(RuntimeError, match="Depth snapshot audit"):
        production_pipeline.run_pipeline(
            pgn_path,
            root=tmp_path / "cgm",
            engine_binary=tmp_path / "stockfish",
        )
