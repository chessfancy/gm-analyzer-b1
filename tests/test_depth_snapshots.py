import chess
import chess.engine

from chessgrandmaster.engine_worker import LucasEngineWorker


class FakeAnalysis:

    def __init__(self, infos):
        self.infos = infos

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def __iter__(self):
        return iter(self.infos)


class FakeEngine:

    def __init__(self, infos):
        self.infos = infos
        self.analysis_calls = []

    def analysis(self, board, limit, **kwargs):
        self.analysis_calls.append((board.copy(), limit, kwargs))
        return FakeAnalysis(self.infos)


def _worker(infos, *, depth=14, multipv=1):
    worker = object.__new__(LucasEngineWorker)
    worker.engine = FakeEngine(infos)
    worker.depth = depth
    worker.time_sec = 0.0
    worker.nodes = 0
    worker.multipv = multipv
    worker.snapshot_depths = (12, 14, 16, 18, 19)
    return worker


def _info(
    *,
    depth,
    score,
    wdl,
    pv,
    multipv=1,
    seldepth=0,
    nodes=0,
    nps=0,
    time=0.0,
):
    return {
        "depth": depth,
        "seldepth": seldepth,
        "multipv": multipv,
        "score": score,
        "wdl": wdl,
        "nodes": nodes,
        "nps": nps,
        "time": time,
        "pv": [chess.Move.from_uci(uci) for uci in pv],
    }


def test_depth_only_limit_omits_time():
    worker = object.__new__(LucasEngineWorker)
    worker.depth = 19
    worker.time_sec = 0.0
    worker.nodes = 0
    limit = worker._limit()
    assert limit.depth == 19
    assert limit.time is None


def test_active_checkpoints_stop_at_final_depth():
    worker = object.__new__(LucasEngineWorker)
    worker.depth = 18
    worker.snapshot_depths = (12, 14, 16, 18, 19)
    assert worker._active_snapshot_depths() == (12, 14, 16, 18)


def test_stream_search_captures_checkpoints_and_latest_multipv_rows():
    infos = [
        _info(
            depth=12,
            score=chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE),
            wdl=chess.engine.PovWdl(
                chess.engine.Wdl(350, 500, 150), chess.WHITE
            ),
            pv=("e2e4", "e7e5"),
            nodes=1200,
        ),
        _info(
            depth=12,
            multipv=2,
            score=chess.engine.PovScore(chess.engine.Cp(10), chess.WHITE),
            wdl=chess.engine.PovWdl(
                chess.engine.Wdl(300, 500, 200), chess.WHITE
            ),
            pv=("d2d4", "d7d5"),
        ),
        _info(
            depth=14,
            score=chess.engine.PovScore(chess.engine.Cp(35), chess.WHITE),
            wdl=chess.engine.PovWdl(
                chess.engine.Wdl(400, 450, 150), chess.WHITE
            ),
            pv=("g1f3", "g8f6"),
            seldepth=18,
            nodes=2400,
            nps=12000,
            time=0.2,
        ),
    ]
    worker = _worker(infos, multipv=2)

    responses, snapshots = worker._stream_search(
        chess.Board(), chess.WHITE, "primary"
    )

    assert len(worker.engine.analysis_calls) == 1
    assert [snapshot.checkpoint_depth for snapshot in snapshots] == [12, 14]
    assert snapshots[0].uci == "e2e4"
    assert snapshots[1].reported_depth == 14
    assert snapshots[1].uci == "g1f3"
    assert snapshots[1].wdl_wins == 400
    assert snapshots[1].wdl_draws == 450
    assert snapshots[1].wdl_losses == 150
    assert [(response.uci, response.depth) for response in responses] == [
        ("g1f3", 14),
        ("d2d4", 12),
    ]


def test_post_move_snapshots_use_original_mover_pov_and_prepended_move():
    infos = [
        _info(
            depth=12,
            score=chess.engine.PovScore(chess.engine.Cp(-42), chess.BLACK),
            wdl=chess.engine.PovWdl(
                chess.engine.Wdl(120, 300, 580), chess.BLACK
            ),
            pv=("e7e5", "g1f3"),
        ),
        _info(
            depth=14,
            score=chess.engine.PovScore(chess.engine.Mate(-3), chess.BLACK),
            wdl=chess.engine.PovWdl(
                chess.engine.Wdl(5, 15, 980), chess.BLACK
            ),
            pv=("e7e5", "g1f3"),
        ),
    ]
    worker = _worker(infos)
    played_move = chess.Move.from_uci("e2e4")
    board_after = chess.Board()
    board_after.push(played_move)

    responses, snapshots = worker._stream_search(
        board_after,
        chess.WHITE,
        "post_move",
        forced_first_move=played_move,
    )

    assert [snapshot.uci for snapshot in snapshots] == ["e2e4", "e2e4"]
    assert snapshots[0].pv_uci == "e2e4 e7e5 g1f3"
    assert snapshots[0].cp == 42
    assert snapshots[0].wdl_wins == 580
    assert snapshots[0].wdl_draws == 300
    assert snapshots[0].wdl_losses == 120
    assert snapshots[1].mate == 4
    assert snapshots[1].wdl_wins == 980
    assert snapshots[1].wdl_losses == 5
    assert responses[0].uci == "e2e4"
    assert responses[0].mate == 4
