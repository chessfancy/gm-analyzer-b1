"""Offline discovery helpers for candidate ranking."""

from .name_filter import (
    AMBIGUOUS_TOKENS,
    VIETNAMESE_NAME_TOKENS,
    VIETNAMESE_SURNAMES,
    VietnameseNameHint,
    is_confirmed_vie_fide_id,
    normalize_player_name,
    vietnamese_name_hint,
)
from .chess_results import (
    ChessResultsCandidate,
    ChessResultsDiscovery,
    CorpusDiscoveryResult,
    CorpusWindow,
    DiscoveryResult,
    PlayerEvidence,
    merge_corpus_priority,
)
from .acquire import (
    CandidateAcquisition,
    DiscoveryAcquisitionResult,
    DiscoveryAcquisitionService,
)

__all__ = [
    "AMBIGUOUS_TOKENS",
    "VIETNAMESE_NAME_TOKENS",
    "VIETNAMESE_SURNAMES",
    "VietnameseNameHint",
    "is_confirmed_vie_fide_id",
    "normalize_player_name",
    "vietnamese_name_hint",
    "ChessResultsCandidate",
    "ChessResultsDiscovery",
    "CorpusDiscoveryResult",
    "CorpusWindow",
    "DiscoveryResult",
    "PlayerEvidence",
    "merge_corpus_priority",
    "CandidateAcquisition",
    "DiscoveryAcquisitionResult",
    "DiscoveryAcquisitionService",
]
