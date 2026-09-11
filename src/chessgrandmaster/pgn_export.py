
import json
import sqlite3
from pathlib import Path

import chess
import chess.pgn

from .engine_manifest import configured_engine


class LucasPGNExporter:

    def __init__(
        self,
        db_path,
        run_id,
        engine_label=None,
        configured_time_sec=3.0,
    ):
        self.db_path = str(db_path)
        self.run_id = run_id
        self.engine_label = (
            engine_label
            or configured_engine()["label"]
        )
        self.configured_time_sec = configured_time_sec


    # --------------------------------------------------
    # Lucas-compatible text formatting
    # --------------------------------------------------

    def _time_label(self):

        text = f"{self.configured_time_sec:.2f}"
        text = text.rstrip("0").rstrip(".")

        return f"{text} Second(s)"


    def _response_text(
        self,
        cp,
        mate,
        mover_is_white,
    ):
        """
        Port of the relevant EngineResponse.texto()
        behaviour used by Lucas PGN export.
        """

        if mate:

            mt = mate

            if mt == 1:
                return (
                    "Black is in checkmate"
                    if mover_is_white
                    else "White is in checkmate"
                )

            if not mover_is_white:
                mt = -mt

            if mt > 1 and mover_is_white:
                mt -= 1

            elif mt < -1 and not mover_is_white:
                mt += 1

            if mate > 0:
                side_white = mover_is_white
            else:
                side_white = not mover_is_white

            side = (
                "White"
                if side_white
                else "Black"
            )

            return f"{side} mates in {abs(mt)}"


        # Our DB stores score from POV of the player
        # who was to move in fen_before.
        # Lucas texto() presents pawn score from
        # White's board perspective.
        white_cp = (
            cp
            if mover_is_white
            else -cp
        )

        return f"{white_cp / 100:+.2f} pawns"


    # --------------------------------------------------
    # PGN tree helper
    # --------------------------------------------------

    def _add_pv(
        self,
        root,
        start_board,
        pv_uci,
        first_comment="",
        mainline=True,
    ):

        tokens = pv_uci.split()

        if not tokens:
            raise ValueError("Empty PV")

        board = start_board.copy()
        node = None

        for index, token in enumerate(tokens):

            move = chess.Move.from_uci(token)

            if move not in board.legal_moves:
                raise ValueError(
                    f"Illegal PV move {token} "
                    f"for {board.fen()}"
                )

            if index == 0:

                if mainline:
                    node = root.add_main_variation(
                        move
                    )
                else:
                    node = root.add_variation(
                        move
                    )

                if first_comment:
                    node.comment = first_comment

            else:

                node = node.add_main_variation(
                    move
                )

            board.push(move)

        return board


    # --------------------------------------------------
    # Engine responses
    # --------------------------------------------------

    def _responses(
        self,
        con,
        analysis_id,
    ):

        rows = con.execute("""
            SELECT
                rank,
                is_played,
                source_search,
                uci,
                cp,
                mate,
                depth,
                nodes,
                time_ms,
                pv_uci
            FROM engine_responses
            WHERE analysis_id=?
            ORDER BY rank
        """, (
            analysis_id,
        )).fetchall()


        responses = []

        for row in rows:

            responses.append({
                "rank": row[0],
                "is_played": bool(row[1]),
                "source_search": row[2],
                "uci": row[3],
                "cp": row[4],
                "mate": row[5],
                "depth": row[6],
                "nodes": row[7],
                "time_ms": row[8],
                "pv_uci": row[9],
            })


        if not responses:
            raise RuntimeError(
                f"No responses for analysis {analysis_id}"
            )


        best = responses[0]

        played = next(
            (
                r
                for r in responses
                if r["is_played"]
            ),
            None,
        )

        if played is None:
            raise RuntimeError(
                f"No played response "
                f"for analysis {analysis_id}"
            )

        return best, played


    # --------------------------------------------------
    # One Lucas-style error slice
    # --------------------------------------------------

    def _build_slice(
        self,
        con,
        headers,
        analysis_id,
        fen_before,
        side,
    ):

        best, played = self._responses(
            con,
            analysis_id,
        )

        mover_is_white = (
            side == "white"
        )

        board = chess.Board(
            fen_before
        )


        game = chess.pgn.Game()
        game.headers.clear()


        # Lucas copies all original tags except
        # RESULT and FEN.
        for key, value in headers.items():

            if key.upper() in (
                "RESULT",
                "FEN",
            ):
                continue

            game.headers[key] = str(value)


        game.headers["FEN"] = fen_before

        # Temporary value. Replaced after best PV.
        game.headers["Result"] = "*"


        # -----------------------------
        # Best engine PV = MAINLINE
        # -----------------------------

        best_text = self._response_text(
            best["cp"],
            best["mate"],
            mover_is_white,
        )

        best_comment = (
            f"{self.engine_label} "
            f"{self._time_label()}: "
            f"{best_text}"
        )


        final_board = self._add_pv(
            game,
            board,
            best["pv_uci"],
            first_comment=best_comment,
            mainline=True,
        )


        # -----------------------------
        # Actual played move +
        # engine continuation = variation
        # -----------------------------

        played_text = self._response_text(
            played["cp"],
            played["mate"],
            mover_is_white,
        )

        self._add_pv(
            game,
            board,
            played["pv_uci"],
            first_comment=played_text,
            mainline=False,
        )


        # Lucas result follows the best-PV game.
        if final_board.is_game_over(
            claim_draw=False
        ):
            result = final_board.result(
                claim_draw=False
            )
        else:
            result = "*"

        game.headers["Result"] = result

        return game


    # --------------------------------------------------
    # Rebuild original full game
    # --------------------------------------------------

    def _build_original(
        self,
        con,
        game_id,
        initial_fen,
        headers,
    ):

        game = chess.pgn.Game()
        game.headers.clear()

        for key, value in headers.items():
            game.headers[key] = str(value)


        # Safety for non-standard starting PGNs.
        if (
            initial_fen != chess.STARTING_FEN
            and "FEN" not in game.headers
        ):
            game.headers["SetUp"] = "1"
            game.headers["FEN"] = initial_fen


        rows = con.execute("""
            SELECT
                m.uci,
                COALESCE(ma.nag, 0)
            FROM moves m

            LEFT JOIN move_analysis ma
              ON ma.move_id = m.id
             AND ma.run_id = ?

            WHERE m.game_id = ?

            ORDER BY m.ply
        """, (
            self.run_id,
            game_id,
        )).fetchall()


        board = chess.Board(
            initial_fen
        )

        node = game

        for uci, nag in rows:

            move = chess.Move.from_uci(
                uci
            )

            if move not in board.legal_moves:
                raise ValueError(
                    f"Illegal original move "
                    f"{uci} for {board.fen()}"
                )

            node = node.add_main_variation(
                move
            )

            if nag:
                node.nags.add(
                    int(nag)
                )

            board.push(move)


        return game


    # --------------------------------------------------
    # Export one category
    # --------------------------------------------------

    def export_category(
        self,
        category,
        output_path,
        include_original=True,
    ):

        output_path = Path(
            output_path
        )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )


        con = sqlite3.connect(
            self.db_path
        )


        games = con.execute("""
            SELECT DISTINCT
                g.id,
                g.source_game_index,
                g.initial_fen,
                g.headers_json

            FROM games g

            JOIN moves m
              ON m.game_id = g.id

            JOIN move_analysis ma
              ON ma.move_id = m.id

            WHERE ma.run_id = ?
              AND ma.category = ?

            ORDER BY g.source_game_index
        """, (
            self.run_id,
            category,
        )).fetchall()


        def render_game(game):
            # IMPORTANT:
            # StringExporter is stateful.
            # Create a fresh exporter for every PGN record.
            exporter = chess.pgn.StringExporter(
                headers=True,
                variations=True,
                comments=True,
                columns=80,
            )
            return game.accept(exporter)


        slice_count = 0
        original_count = 0


        with open(
            output_path,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as out:

            for (
                game_id,
                game_index,
                initial_fen,
                headers_json,
            ) in games:

                headers = json.loads(
                    headers_json
                )


                errors = con.execute("""
                    SELECT
                        ma.id,
                        m.ply,
                        m.fen_before,
                        m.side

                    FROM move_analysis ma

                    JOIN moves m
                      ON m.id = ma.move_id

                    WHERE ma.run_id = ?
                      AND m.game_id = ?
                      AND ma.category = ?

                    ORDER BY m.ply
                """, (
                    self.run_id,
                    game_id,
                    category,
                )).fetchall()


                # Lucas writes all error slices
                # for this game first.
                for (
                    analysis_id,
                    ply,
                    fen_before,
                    side,
                ) in errors:

                    game_slice = (
                        self._build_slice(
                            con,
                            headers,
                            analysis_id,
                            fen_before,
                            side,
                        )
                    )

                    out.write(
                        render_game(game_slice)
                    )

                    out.write("\n\n")

                    slice_count += 1


                # Then exactly one original game.
                if include_original:

                    original = (
                        self._build_original(
                            con,
                            game_id,
                            initial_fen,
                            headers,
                        )
                    )

                    out.write(
                        render_game(original)
                    )

                    out.write("\n\n")

                    original_count += 1


        con.close()


        return {
            "category": category,
            "slices": slice_count,
            "original_games": original_count,
            "records": (
                slice_count
                + original_count
            ),
            "path": str(output_path),
        }
