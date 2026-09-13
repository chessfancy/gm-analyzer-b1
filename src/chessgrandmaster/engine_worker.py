
from dataclasses import dataclass
import chess
import chess.engine

from . import lucas_eval


@dataclass
class EngineResponse:
    uci: str
    cp: int
    mate: int
    depth: int
    seldepth: int
    nodes: int
    nps: int
    time_ms: int
    pv_uci: str
    source_search: str
    is_played: bool = False
    rank: int = -1


@dataclass
class DepthSnapshot:
    source_search: str
    checkpoint_depth: int
    reported_depth: int
    uci: str
    cp: int
    mate: int
    wdl_wins: int | None
    wdl_draws: int | None
    wdl_losses: int | None
    seldepth: int
    nodes: int
    nps: int
    time_ms: int
    pv_uci: str


@dataclass
class AnalysisResult:
    fen_before: str
    played_uci: str
    responses: list
    played_rank: int
    lucas_loss: float
    category: str
    nag: int
    second_search: bool
    depth_snapshots: list


def _sort_key(r):
    # Port of Lucas EngineResponse._orden_key()
    if r.mate > 0:
        return (2, -r.mate)

    if r.mate < 0:
        return (0, -r.mate)

    return (1, r.cp)


def _score_parts(score):
    if score.is_mate():
        return 0, score.mate()

    return score.score(), 0


class LucasEngineWorker:

    def __init__(
        self,
        engine_path,
        threads=1,
        hash_mb=256,
        multipv=1,
        depth=18,
        time_sec=0.0,
        nodes=0,
        snapshot_depths=(12, 14, 16, 18, 19),
    ):
        self.engine_path = str(engine_path)

        self.threads = threads
        self.hash_mb = hash_mb
        self.multipv = multipv

        self.depth = depth
        self.time_sec = time_sec
        self.nodes = nodes
        self.snapshot_depths = tuple(snapshot_depths)

        # Persistent Stockfish process.
        self.engine = chess.engine.SimpleEngine.popen_uci(
            self.engine_path
        )

        self.engine.configure({
            "Threads": self.threads,
            "Hash": self.hash_mb,
            "UCI_ShowWDL": True,
        })


    def close(self):
        if self.engine is not None:
            self.engine.quit()
            self.engine = None


    def __enter__(self):
        return self


    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


    def _limit(self, depth=None):
        kwargs = {}

        use_depth = self.depth if depth is None else depth

        if use_depth:
            kwargs["depth"] = use_depth

        if self.time_sec:
            kwargs["time"] = self.time_sec

        if self.nodes:
            kwargs["nodes"] = self.nodes

        return chess.engine.Limit(**kwargs)


    def _active_snapshot_depths(self, final_depth=None):
        if final_depth is None:
            final_depth = self.depth

        return tuple(
            checkpoint
            for checkpoint in self.snapshot_depths
            if not final_depth or checkpoint <= final_depth
        )


    def _response_from_info(
        self,
        info,
        pov_color,
        source_search,
        forced_first_move=None,
    ):
        score = info["score"].pov(pov_color)

        cp, mate = _score_parts(score)

        # Lucas R6 EngineResponse.change_side():
        # after a post-move search the score is flipped back
        # to the original mover's POV.
        #
        # If that produces a positive mate, Lucas adds 1
        # because the actual played move is inserted in front
        # of the post-move PV.
        if forced_first_move is not None and mate > 0:
            mate += 1

        pv = list(info.get("pv", []))

        # Search #2 starts AFTER the played move.
        # Lucas prepends the played move back onto that PV.
        if forced_first_move is not None:
            pv = [forced_first_move] + pv

        uci = pv[0].uci() if pv else ""

        return EngineResponse(
            uci=uci,
            cp=cp,
            mate=mate,
            depth=info.get("depth", 0),
            seldepth=info.get("seldepth", 0),
            nodes=info.get("nodes", 0),
            nps=info.get("nps", 0),
            time_ms=int(info.get("time", 0) * 1000),
            pv_uci=" ".join(m.uci() for m in pv),
            source_search=source_search,
        )


    def _snapshot_from_info(
        self,
        info,
        pov_color,
        source_search,
        checkpoint_depth,
        forced_first_move=None,
    ):
        response = self._response_from_info(
            info,
            pov_color,
            source_search,
            forced_first_move=forced_first_move,
        )

        wdl = info.get("wdl")

        if wdl is not None:
            wdl = wdl.pov(pov_color)

        return DepthSnapshot(
            source_search=source_search,
            checkpoint_depth=checkpoint_depth,
            reported_depth=response.depth,
            uci=response.uci,
            cp=response.cp,
            mate=response.mate,
            wdl_wins=wdl.wins if wdl is not None else None,
            wdl_draws=wdl.draws if wdl is not None else None,
            wdl_losses=wdl.losses if wdl is not None else None,
            seldepth=response.seldepth,
            nodes=response.nodes,
            nps=response.nps,
            time_ms=response.time_ms,
            pv_uci=response.pv_uci,
        )


    def _stream_search(
        self,
        board,
        pov_color,
        source_search,
        forced_first_move=None,
        search_depth=None,
    ):
        latest_infos = {}
        snapshots = []
        captured_depths = set()
        active_depths = self._active_snapshot_depths(search_depth)

        with self.engine.analysis(
            board,
            self._limit(depth=search_depth),
            multipv=self.multipv if forced_first_move is None else 1,
            info=chess.engine.INFO_ALL,
        ) as analysis:
            for info in analysis:
                multipv = info.get("multipv", 1)
                aggregate = latest_infos.setdefault(multipv, {})
                aggregate.update(info)

                if "score" not in aggregate:
                    continue

                if multipv != 1:
                    continue

                snapshot_info = dict(aggregate)
                reported_depth = snapshot_info.get("depth", 0)

                for checkpoint in active_depths:
                    if (
                        checkpoint not in captured_depths
                        and reported_depth >= checkpoint
                    ):
                        snapshots.append(self._snapshot_from_info(
                            snapshot_info,
                            pov_color,
                            source_search,
                            checkpoint,
                            forced_first_move=forced_first_move,
                        ))
                        captured_depths.add(checkpoint)

        responses = [
            self._response_from_info(
                dict(latest_infos[multipv]),
                pov_color,
                source_search,
                forced_first_move=forced_first_move,
            )
            for multipv in sorted(latest_infos)
        ]

        return responses, snapshots


    def analyze_move(self, fen_before, played_uci):

        board = chess.Board(fen_before)

        pov_color = board.turn

        played_move = chess.Move.from_uci(played_uci)

        if played_move not in board.legal_moves:
            raise ValueError(
                f"Illegal played move {played_uci} "
                f"for FEN {fen_before}"
            )

        # -------------------------------------------
        # SEARCH #1
        # -------------------------------------------

        responses, depth_snapshots = self._stream_search(
            board,
            pov_color,
            "primary",
        )

        played_response = None

        for response in responses:
            if response.uci == played_uci:
                response.is_played = True
                played_response = response
                break


        # -------------------------------------------
        # SEARCH #2
        # Only if actual move was not in Search #1
        # -------------------------------------------

        second_search = False

        if played_response is None:

            second_search = True

            board_after = board.copy()
            board_after.push(played_move)

            second_depth = self.depth

            if (
                not self.depth
                and not self.nodes
                and responses
                and responses[0].depth > 1
            ):
                second_depth = responses[0].depth - 1

            post_move_responses, post_move_snapshots = self._stream_search(
                board_after,
                pov_color,
                "post_move",
                forced_first_move=played_move,
                search_depth=second_depth,
            )

            played_response = post_move_responses[0]

            played_response.is_played = True

            responses.append(played_response)
            depth_snapshots.extend(post_move_snapshots)


        # -------------------------------------------
        # Lucas orders all responses again
        # -------------------------------------------

        responses.sort(
            key=_sort_key,
            reverse=True
        )

        for rank, response in enumerate(responses):
            response.rank = rank


        played_rank = next(
            r.rank
            for r in responses
            if r.is_played
        )

        best = responses[0]
        played = responses[played_rank]


        # -------------------------------------------
        # Lucas classification
        # -------------------------------------------

        loss, category, nag = lucas_eval.classify(
            best_cp=best.cp,
            best_mate=best.mate,
            played_cp=played.cp,
            played_mate=played.mate,
        )


        return AnalysisResult(
            fen_before=fen_before,
            played_uci=played_uci,
            responses=responses,
            played_rank=played_rank,
            lucas_loss=loss,
            category=category,
            nag=nag,
            second_search=second_search,
            depth_snapshots=depth_snapshots,
        )
