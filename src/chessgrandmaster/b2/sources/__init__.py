"""Source adapters for the B2 acquisition boundary."""

from .base import SourceAdapter, SourceDescriptor, SourceRef
from .chess_results import ChessResultsAdapter, PgnAvailability

__all__ = [
    "ChessResultsAdapter",
    "PgnAvailability",
    "SourceAdapter",
    "SourceDescriptor",
    "SourceRef",
]
