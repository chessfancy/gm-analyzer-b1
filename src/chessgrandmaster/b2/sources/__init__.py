"""Source adapters for the B2 acquisition boundary."""

from .base import SourceAdapter, SourceDescriptor, SourceRef
from .chesscom_broadcast import (
    Chess24Adapter,
    ChessComAdapter,
    ChessComBroadcastAdapter,
    ChessComChess24Adapter,
)
from .chess_results import ChessResultsAdapter, PgnAvailability
from .lichess_broadcast import LichessAdapter, LichessBroadcastAdapter
from .twic import TWICAdapter, TwicAdapter

__all__ = [
    "ChessResultsAdapter",
    "Chess24Adapter",
    "ChessComAdapter",
    "ChessComBroadcastAdapter",
    "ChessComChess24Adapter",
    "LichessAdapter",
    "LichessBroadcastAdapter",
    "PgnAvailability",
    "SourceAdapter",
    "SourceDescriptor",
    "SourceRef",
    "TWICAdapter",
    "TwicAdapter",
]
