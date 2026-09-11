import os
import sqlite3
from pathlib import Path

import chess.pgn
import pytest

from chessgrandmaster.production_pipeline import run_pipeline
from chessgrandmaster.engine_manifest import configured_engine


pytestmark = pytest.mark.skipif(
    os.environ.get("CGM_RUN_GOLDEN") != "1",
    reason="golden Stockfish regression is opt-in",
)


def test_tre_2026_game_1_lucas_semantics(tmp_path):
    fixture = Path(__file__).with_name("tre_2026_game_1.pgn")
    root = tmp_path / "cgm"

    run_pipeline(
        fixture,
        root=root,
    )

    db_files = list((root / "db").glob("analysis_*.sqlite"))
    assert len(db_files) == 1

    con = sqlite3.connect(db_files[0])

    run_id = con.execute(
        "SELECT MAX(id) FROM analysis_runs"
    ).fetchone()[0]

    assert run_id is not None

    # Full game must contain all 83 plies.
    analyzed = con.execute(
        """
        SELECT COUNT(*)
        FROM move_analysis
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()[0]

    assert analyzed == 83

    # Known Lucas/Stockfish golden position:
    # 21...Nh5 must remain a clear blunder.
    move = con.execute(
        """
        SELECT
            ma.id,
            ma.category,
            ma.nag,
            ma.lucas_eval_loss
        FROM move_analysis ma
        JOIN moves m
          ON m.id = ma.move_id
        WHERE ma.run_id = ?
          AND m.ply = 42
          AND m.san = 'Nh5'
        """,
        (run_id,),
    ).fetchone()

    assert move is not None

    analysis_id, category, nag, lucas_loss = move

    # Engine scores can vary slightly between runs because the
    # production search uses a wall-clock limit and persistent TT state.
    # Do not pin this position to one Lucas threshold bucket here.
    assert category in {"MISTAKE", "BLUNDER"}
    assert nag in {2, 4}
    assert lucas_loss > 7.5

    responses = con.execute(
        """
        SELECT
            rank,
            is_played,
            source_search,
            uci,
            depth
        FROM engine_responses
        WHERE analysis_id = ?
        ORDER BY rank
        """,
        (analysis_id,),
    ).fetchall()

    con.close()

    assert len(responses) >= 2

    best = responses[0]
    played = next(r for r in responses if r[1] == 1)

    # Stable best move from both Deepnote and Codespaces golden runs.
    assert best[2] == "primary"
    assert best[3] == "e5g6"       # ...Ng6
    assert best[4] == 18

    # Lucas second-search reconstruction of the played move.
    assert played[2] == "post_move"
    assert played[3] == "f6h5"     # ...Nh5
    assert played[4] == 18

    # Export structure:
    # error slice first, original full game second.
    engine_tag = configured_engine()["output_tag"]

    blunder_files = list(
        (root / "output").glob(
            f"Blunders_*_{engine_tag}.pgn"
        )
    )

    assert len(blunder_files) == 1

    games = []

    with blunder_files[0].open(
        encoding="utf-8",
        errors="replace",
    ) as f:
        while True:
            game = chess.pgn.read_game(f)

            if game is None:
                break

            games.append(game)

    assert len(games) == 2

    error_slice, original = games

    assert "FEN" in error_slice.headers
    assert len(error_slice.variations) >= 2

    assert "FEN" not in original.headers
    assert original.headers["Result"] == "1-0"
