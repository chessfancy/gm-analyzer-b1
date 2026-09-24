import gzip
import json

import chess
import chess.engine

from chessgrandmaster.engine_worker import LucasEngineWorker
from chessgrandmaster.uci_telemetry import UciEventArchive


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


class SequencedFakeEngine:

    def __init__(self, streams):
        self.streams = iter(streams)
        self.analysis_calls = []

    def analysis(self, board, limit, **kwargs):
        self.analysis_calls.append((board.copy(), limit, kwargs))
        return FakeAnalysis(next(self.streams))


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
    hashfull=None,
    tbhits=None,
):
    info = {
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
    if hashfull is not None:
        info["hashfull"] = hashfull
    if tbhits is not None:
        info["tbhits"] = tbhits
    return info


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


def test_stream_search_accumulates_split_info_fields_by_multipv_rank():
    first = _info(
        depth=11,
        score=chess.engine.PovScore(chess.engine.Cp(20), chess.WHITE),
        wdl=chess.engine.PovWdl(
            chess.engine.Wdl(350, 500, 150), chess.WHITE
        ),
        pv=("e2e4", "e7e5"),
        seldepth=16,
        nodes=1200,
        nps=6000,
        time=0.2,
    )
    second = {
        "depth": 12,
        "multipv": 1,
        "score": chess.engine.PovScore(
            chess.engine.Cp(31), chess.WHITE
        ),
    }
    worker = _worker([first, second], depth=12)

    responses, snapshots = worker._stream_search(
        chess.Board(), chess.WHITE, "primary"
    )

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.reported_depth == 12
    assert snapshot.cp == 31
    assert snapshot.pv_uci == "e2e4 e7e5"
    assert (
        snapshot.wdl_wins,
        snapshot.wdl_draws,
        snapshot.wdl_losses,
    ) == (350, 500, 150)
    assert (snapshot.seldepth, snapshot.nodes, snapshot.nps) == (
        16,
        1200,
        6000,
    )
    assert snapshot.time_ms == 200
    info_json = json.loads(snapshot.info_json)
    assert info_json["depth"] == 12
    assert info_json["seldepth"] == 16
    assert info_json["nodes"] == 1200
    assert info_json["nps"] == 6000
    assert info_json["time"] == 0.2
    assert info_json["pv"] == ["e2e4", "e7e5"]
    assert responses[0].cp == 31
    assert responses[0].pv_uci == "e2e4 e7e5"
    assert (responses[0].nodes, responses[0].nps) == (1200, 6000)


def test_final_checkpoint_uses_latest_info_but_intermediate_keeps_first_crossing():
    depth_18 = _info(
        depth=18,
        score=chess.engine.PovScore(chess.engine.Cp(18), chess.WHITE),
        wdl=chess.engine.PovWdl(chess.engine.Wdl(300, 500, 200), chess.WHITE),
        pv=("d2d4", "d7d5"),
        seldepth=22,
        nodes=1800,
        nps=9000,
        time=0.18,
        hashfull=180,
        tbhits=1,
    )
    depth_19_a = _info(
        depth=19,
        score=chess.engine.PovScore(chess.engine.Cp(30), chess.WHITE),
        wdl=chess.engine.PovWdl(chess.engine.Wdl(320, 480, 200), chess.WHITE),
        pv=("g1f3", "g8f6"),
        seldepth=24,
        nodes=1900,
        nps=9500,
        time=0.19,
        hashfull=190,
        tbhits=2,
    )
    depth_19_b = _info(
        depth=19,
        score=chess.engine.PovScore(chess.engine.Cp(45), chess.WHITE),
        wdl=chess.engine.PovWdl(chess.engine.Wdl(410, 390, 200), chess.WHITE),
        pv=("c2c4", "e7e5"),
        seldepth=27,
        nodes=2100,
        nps=10500,
        time=0.21,
        hashfull=210,
        tbhits=4,
    )
    worker = _worker([depth_18, depth_19_a, depth_19_b], depth=19)

    responses, snapshots = worker._stream_search(
        chess.Board(), chess.WHITE, "primary"
    )

    by_depth = {snapshot.checkpoint_depth: snapshot for snapshot in snapshots}
    assert by_depth[18].cp == 18
    assert by_depth[18].pv_uci == "d2d4 d7d5"
    assert by_depth[19].cp == responses[0].cp == 45
    assert by_depth[19].mate == responses[0].mate == 0
    assert by_depth[19].uci == responses[0].uci == "c2c4"
    assert by_depth[19].pv_uci == responses[0].pv_uci == "c2c4 e7e5"
    assert (by_depth[19].wdl_wins, by_depth[19].wdl_draws, by_depth[19].wdl_losses) == (410, 390, 200)
    assert (by_depth[19].seldepth, by_depth[19].nodes, by_depth[19].nps) == (27, 2100, 10500)
    assert by_depth[19].time_ms == 210
    assert (by_depth[19].hashfull, by_depth[19].tbhits) == (210, 4)
    assert json.loads(by_depth[19].info_json)["nodes"] == 2100


def test_currmove_only_events_cannot_capture_intermediate_checkpoints():
    infos = [
        _info(
            depth=15,
            score=chess.engine.PovScore(chess.engine.Cp(15), chess.WHITE),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(300, 500, 200), chess.WHITE),
            pv=("e2e4", "e7e5"),
            nodes=1500,
            nps=7500,
            time=0.15,
        ),
        {
            "depth": 16,
            "multipv": 1,
            "currmove": chess.Move.from_uci("a2a3"),
            "currmovenumber": 1,
        },
        _info(
            depth=16,
            score=chess.engine.PovScore(chess.engine.Cp(16), chess.WHITE),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(320, 480, 200), chess.WHITE),
            pv=("d2d4", "d7d5"),
            seldepth=20,
            nodes=1600,
            nps=8000,
            time=0.16,
            hashfull=160,
            tbhits=2,
        ),
        _info(
            depth=17,
            score=chess.engine.PovScore(chess.engine.Cp(17), chess.WHITE),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(340, 470, 190), chess.WHITE),
            pv=("g1f3", "g8f6"),
            nodes=1700,
            nps=8500,
            time=0.17,
        ),
        {
            "depth": 18,
            "multipv": 1,
            "currmove": chess.Move.from_uci("b1c3"),
            "currmovenumber": 2,
        },
        _info(
            depth=18,
            score=chess.engine.PovScore(chess.engine.Cp(18), chess.WHITE),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(360, 460, 180), chess.WHITE),
            pv=("c2c4", "e7e5"),
            seldepth=22,
            nodes=1800,
            nps=9000,
            time=0.18,
            hashfull=180,
            tbhits=3,
        ),
        _info(
            depth=19,
            score=chess.engine.PovScore(chess.engine.Cp(19), chess.WHITE),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(380, 450, 170), chess.WHITE),
            pv=("g2g3", "d7d6"),
            nodes=1900,
            nps=9500,
            time=0.19,
        ),
    ]
    worker = _worker(infos, depth=19)
    worker.snapshot_depths = (16, 18, 19)

    _, snapshots = worker._stream_search(
        chess.Board(), chess.WHITE, "primary"
    )

    by_depth = {snapshot.checkpoint_depth: snapshot for snapshot in snapshots}
    assert by_depth[16].cp == 16
    assert by_depth[16].pv_uci == "d2d4 d7d5"
    assert (by_depth[16].nodes, by_depth[16].nps, by_depth[16].time_ms) == (
        1600,
        8000,
        160,
    )
    assert by_depth[18].cp == 18
    assert by_depth[18].pv_uci == "c2c4 e7e5"
    assert (by_depth[18].nodes, by_depth[18].nps, by_depth[18].time_ms) == (
        1800,
        9000,
        180,
    )


def test_currmove_only_events_remain_in_raw_gzip_archive(tmp_path):
    path = tmp_path / "uci" / "worker.jsonl.gz"
    archive = UciEventArchive(path, archive_root=tmp_path)
    worker = _worker([
        _info(
            depth=15,
            score=chess.engine.PovScore(chess.engine.Cp(15), chess.WHITE),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(300, 500, 200), chess.WHITE),
            pv=("e2e4",),
        ),
        {
            "depth": 16,
            "multipv": 1,
            "currmove": chess.Move.from_uci("a2a3"),
            "currmovenumber": 1,
        },
        _info(
            depth=16,
            score=chess.engine.PovScore(chess.engine.Cp(16), chess.WHITE),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(320, 480, 200), chess.WHITE),
            pv=("d2d4",),
        ),
    ], depth=16)
    worker.snapshot_depths = (16,)
    worker.archive = archive

    worker._stream_search(chess.Board(), chess.WHITE, "primary")
    archive.close()

    with gzip.open(path, "rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]

    assert any(
        record["event"] == "info"
        and record["info"].get("currmove") == "a2a3"
        and record["info"].get("currmovenumber") == 1
        for record in records
    )


def test_terminal_post_move_mate_given_becomes_mate_in_one():
    worker = _worker([], depth=19)
    played_move = chess.Move.from_uci("d3d1")

    info = {
        "depth": 0,
        "multipv": 1,
        "score": chess.engine.PovScore(chess.engine.Mate(0), chess.WHITE),
        "pv": [],
    }

    response = worker._response_from_info(
        info,
        chess.BLACK,
        "post_move",
        forced_first_move=played_move,
    )

    assert response.uci == "d3d1"
    assert response.pv_uci == "d3d1"
    assert response.cp == 0
    assert response.mate == 1


def test_checkmating_played_move_is_not_rated_as_blunder():
    fen_before = "4R3/6pp/1k6/1p2p3/2n5/3r4/4q1PP/K7 b - - 5 37"
    primary = [
        _info(
            depth=19,
            score=chess.engine.PovScore(chess.engine.Mate(1), chess.BLACK),
            wdl=None,
            pv=("e2b2",),
        )
    ]

    worker = _worker([], depth=19)
    worker.engine = SequencedFakeEngine([primary])

    result = worker.analyze_move(fen_before, "d3d1")

    played = next(response for response in result.responses if response.is_played)
    assert len(worker.engine.analysis_calls) == 1
    assert result.second_search is False
    assert played.uci == "d3d1"
    assert played.pv_uci == "d3d1"
    assert played.source_search == "post_move"
    assert played.mate == 1
    synthetic_info = json.loads(played.info_json)
    assert synthetic_info["cgm_synthetic"] is True
    assert synthetic_info["cgm_reason"] == "terminal_post_move"
    assert synthetic_info["cgm_terminal"] == "checkmate"
    assert synthetic_info["pv"] == ["d3d1"]
    assert result.lucas_loss == 0
    assert result.category == "NO_RATING"
    assert result.nag == 0


def test_olympiad_qh7_mate_does_not_launch_terminal_post_move_search():
    fen_before = (
        "r1b2qnQ/pp4p1/1b2p1k1/1P1pPrN1/4n3/"
        "P7/2P3PP/R1B2R1K w - - 0 25"
    )
    primary = [
        _info(
            depth=19,
            score=chess.engine.PovScore(chess.engine.Cp(500), chess.WHITE),
            wdl=None,
            pv=("h8g8",),
        )
    ]

    worker = _worker([], depth=19)
    worker.engine = SequencedFakeEngine([primary])

    result = worker.analyze_move(fen_before, "h8h7")

    played = next(response for response in result.responses if response.is_played)
    assert len(worker.engine.analysis_calls) == 1
    assert result.second_search is False
    assert played.uci == "h8h7"
    assert played.pv_uci == "h8h7"
    assert played.mate == 1
    assert played.depth == 0
    assert played.nodes == 0
    assert result.played_rank == 0
    assert result.lucas_loss == 0
    assert result.category == "NO_RATING"
    assert result.nag == 0


def test_stalemating_played_move_does_not_launch_terminal_post_move_search():
    fen_before = "8/8/8/8/8/k7/8/KQ6 w - - 0 1"
    primary = [
        _info(
            depth=19,
            score=chess.engine.PovScore(chess.engine.Cp(900), chess.WHITE),
            wdl=None,
            pv=("b1b2",),
        )
    ]

    worker = _worker([], depth=19)
    worker.engine = SequencedFakeEngine([primary])

    result = worker.analyze_move(fen_before, "b1b5")

    played = next(response for response in result.responses if response.is_played)
    assert len(worker.engine.analysis_calls) == 1
    assert result.second_search is False
    assert played.uci == "b1b5"
    assert played.cp == 0
    assert played.mate == 0
    assert played.depth == 0
    assert played.nodes == 0


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


def test_post_move_final_checkpoint_uses_latest_accumulated_info():
    played_move = chess.Move.from_uci("e2e4")
    board_after = chess.Board()
    board_after.push(played_move)
    infos = [
        _info(
            depth=18,
            score=chess.engine.PovScore(chess.engine.Cp(-42), chess.BLACK),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(120, 300, 580), chess.BLACK),
            pv=("e7e5", "g1f3"),
            nodes=1800,
        ),
        _info(
            depth=19,
            score=chess.engine.PovScore(chess.engine.Cp(-30), chess.BLACK),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(160, 300, 540), chess.BLACK),
            pv=("e7e5", "g1f3"),
            nodes=1900,
        ),
        _info(
            depth=19,
            score=chess.engine.PovScore(chess.engine.Cp(-45), chess.BLACK),
            wdl=chess.engine.PovWdl(chess.engine.Wdl(200, 300, 500), chess.BLACK),
            pv=("c7c5", "g1f3"),
            nodes=2100,
        ),
    ]
    worker = _worker(infos, depth=19)

    responses, snapshots = worker._stream_search(
        board_after,
        chess.WHITE,
        "post_move",
        forced_first_move=played_move,
    )

    by_depth = {snapshot.checkpoint_depth: snapshot for snapshot in snapshots}
    assert by_depth[18].cp == 42
    assert by_depth[18].pv_uci == "e2e4 e7e5 g1f3"
    assert by_depth[19].cp == responses[0].cp == 45
    assert by_depth[19].uci == responses[0].uci == "e2e4"
    assert by_depth[19].pv_uci == responses[0].pv_uci == "e2e4 c7c5 g1f3"
    assert (by_depth[19].wdl_wins, by_depth[19].wdl_draws, by_depth[19].wdl_losses) == (500, 300, 200)
    assert by_depth[19].nodes == 2100


def test_time_only_post_move_search_targets_primary_depth_minus_one():
    primary = [
        _info(
            depth=15,
            score=chess.engine.PovScore(chess.engine.Cp(40), chess.WHITE),
            wdl=None,
            pv=("d2d4", "d7d5"),
        )
    ]
    post_move = [
        _info(
            depth=14,
            score=chess.engine.PovScore(chess.engine.Cp(-10), chess.BLACK),
            wdl=None,
            pv=("e7e5", "g1f3"),
        )
    ]
    worker = _worker([], depth=0)
    worker.engine = SequencedFakeEngine([primary, post_move])
    worker.time_sec = 3.0

    worker.analyze_move(chess.STARTING_FEN, "e2e4")

    assert len(worker.engine.analysis_calls) == 2
    post_move_limit = worker.engine.analysis_calls[1][1]
    assert post_move_limit.depth == 14
    assert post_move_limit.time == 3.0
