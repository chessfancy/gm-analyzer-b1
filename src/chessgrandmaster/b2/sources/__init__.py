"""Source adapters for the B2 acquisition boundary."""

from .base import SourceAdapter, SourceDescriptor, SourceRef
from .chess_results import ChessResultsAdapter

__all__ = [
    "ChessResultsAdapter",
    "SourceAdapter",
    "SourceDescriptor",
    "SourceRef",
]
