"""Chess-content identity primitives for exact B2 game deduplication."""

from dataclasses import dataclass
import hashlib

import chess
import chess.pgn


FINGERPRINT_VERSION = "game_fingerprint_v1"


@dataclass(frozen=True)
class GameIdentity:
    """The stable chess-content identity of one parsed PGN game."""

    fingerprint_version: str
    fingerprint: str
    variant: str
    initial_fen: str
    mainline_uci: tuple[str, ...]
    ply_count: int


def normalize_initial_fen(board: chess.Board) -> str:
    """Normalize the initial chess state used by the v1 fingerprint.

    Move counters are omitted because they describe PGN history/formatting,
    while piece placement, side to move, castling, and en-passant state are
    part of the chess position.
    """
    return " ".join(board.fen(en_passant="fen").split()[:4])


def _variant_name(game: chess.pgn.Game) -> str:
    value = game.headers.get("Variant", "Standard").strip().casefold()
    return value or "standard"


def identify_game(game: chess.pgn.Game) -> GameIdentity:
    """Compute the exact v1 identity from variant, start state, and mainline."""
    board = game.board()
    initial_fen = normalize_initial_fen(board)
    moves = tuple(move.uci() for move in game.mainline_moves())
    variant = _variant_name(game)
    payload = "\n".join((variant, initial_fen, *moves)).encode("utf-8")
    fingerprint = hashlib.sha256(payload).hexdigest()

    return GameIdentity(
        fingerprint_version=FINGERPRINT_VERSION,
        fingerprint=fingerprint,
        variant=variant,
        initial_fen=initial_fen,
        mainline_uci=moves,
        ply_count=len(moves),
    )
