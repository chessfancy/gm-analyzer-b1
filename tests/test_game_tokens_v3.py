import chess
import chess.engine

from chessgrandmaster.engine_worker import LucasEngineWorker


class _Analysis:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def __iter__(self):
        return iter([
            {
                "multipv": 1,
                "depth": 12,
                "score": chess.engine.PovScore(
                    chess.engine.Cp(10), chess.WHITE
                ),
                "pv": [chess.Move.from_uci("e2e4")],
            }
        ])


class _Engine:
    def __init__(self):
        self.calls = []

    def analysis(self, board, limit, **kwargs):
        self.calls.append(kwargs)
        return _Analysis()


def _worker():
    worker = object.__new__(LucasEngineWorker)
    worker.engine = _Engine()
    worker.depth = 12
    worker.time_sec = 0.0
    worker.nodes = 0
    worker.multipv = 1
    worker.snapshot_depths = (12,)
    worker.archive = None
    return worker


def test_primary_post_move_and_next_ply_share_one_game_token():
    worker = _worker()
    token = object()

    worker._stream_search(
        chess.Board(), chess.WHITE, "primary", game_token=token
    )
    worker._stream_search(
        chess.Board(), chess.WHITE, "post_move", game_token=token
    )
    worker._stream_search(
        chess.Board(), chess.WHITE, "primary", game_token=token
    )

    assert [call["game"] for call in worker.engine.calls] == [
        token,
        token,
        token,
    ]


def test_switching_to_another_pgn_game_uses_a_different_token():
    worker = _worker()
    first_token = object()
    second_token = object()

    worker._stream_search(
        chess.Board(), chess.WHITE, "primary", game_token=first_token
    )
    worker._stream_search(
        chess.Board(), chess.WHITE, "primary", game_token=second_token
    )

    assert worker.engine.calls[0]["game"] is first_token
    assert worker.engine.calls[1]["game"] is second_token
