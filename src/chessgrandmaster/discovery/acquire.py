"""Bridge discovered Chess-Results candidates into the accepted B2a service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from chessgrandmaster.b2.acquisition import AcquisitionService
from chessgrandmaster.b2.registry import Registry

from .chess_results import ChessResultsCandidate


_COMPLETED_STATUSES = frozenset({"CANONICALIZED", "SHARDED", "READY"})


def _error_message(error: BaseException) -> str:
    message = " ".join(str(error).split())
    return message or type(error).__name__


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CandidateAcquisition:
    """Outcome for one unique provider candidate."""

    provider: str
    external_id: str
    source_url: str
    action: str
    tournament_id: int | None
    tournament_status: str | None
    raw_sha256: str | None
    revision_id: int | None
    error_type: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "external_id": self.external_id,
            "source_url": self.source_url,
            "action": self.action,
            "tournament_id": self.tournament_id,
            "tournament_status": self.tournament_status,
            "raw_sha256": self.raw_sha256,
            "revision_id": self.revision_id,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class DiscoveryAcquisitionResult:
    """JSON-compatible summary for one discovery-to-acquisition batch."""

    discovered: int
    attempted: int
    acquired: int
    skipped_existing: int
    failed: int
    results: tuple[CandidateAcquisition, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "discovered": self.discovered,
            "attempted": self.attempted,
            "acquired": self.acquired,
            "skipped_existing": self.skipped_existing,
            "failed": self.failed,
            "results": [result.to_dict() for result in self.results],
        }


class DiscoveryAcquisitionService:
    """Idempotently route discovered candidates through B2a."""

    def __init__(
        self,
        registry: Registry,
        acquisition: AcquisitionService,
    ) -> None:
        self.registry = registry
        self.acquisition = acquisition

    @staticmethod
    def _unique_candidates(
        candidates: Sequence[ChessResultsCandidate],
    ) -> tuple[ChessResultsCandidate, ...]:
        unique: list[ChessResultsCandidate] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates:
            identity = (candidate.provider, candidate.external_id)
            if identity in seen:
                continue
            seen.add(identity)
            unique.append(candidate)
        return tuple(unique)

    @staticmethod
    def _existing_result(
        candidate: ChessResultsCandidate,
        existing: dict[str, object],
    ) -> CandidateAcquisition:
        return CandidateAcquisition(
            provider=candidate.provider,
            external_id=candidate.external_id,
            source_url=candidate.source_url,
            action="skipped_existing",
            tournament_id=_optional_int(existing.get("tournament_id")),
            tournament_status=(
                None
                if existing.get("status") is None
                else str(existing["status"])
            ),
            raw_sha256=None,
            revision_id=None,
        )

    @staticmethod
    def _acquired_result(
        candidate: ChessResultsCandidate,
        acquisition_result: object,
    ) -> CandidateAcquisition:
        status = getattr(acquisition_result, "tournament_status", None)
        return CandidateAcquisition(
            provider=candidate.provider,
            external_id=candidate.external_id,
            source_url=candidate.source_url,
            action="acquired",
            tournament_id=_optional_int(
                getattr(acquisition_result, "tournament_id", None)
            ),
            tournament_status=None if status is None else str(status),
            raw_sha256=(
                None
                if getattr(acquisition_result, "raw_sha256", None) is None
                else str(acquisition_result.raw_sha256)
            ),
            revision_id=_optional_int(
                getattr(acquisition_result, "revision_id", None)
            ),
        )

    @staticmethod
    def _failed_result(
        candidate: ChessResultsCandidate,
        error: BaseException,
        existing: dict[str, object] | None,
    ) -> CandidateAcquisition:
        return CandidateAcquisition(
            provider=candidate.provider,
            external_id=candidate.external_id,
            source_url=candidate.source_url,
            action="failed",
            tournament_id=(
                None
                if existing is None
                else _optional_int(existing.get("tournament_id"))
            ),
            tournament_status=(
                None
                if existing is None or existing.get("status") is None
                else str(existing["status"])
            ),
            raw_sha256=None,
            revision_id=None,
            error_type=type(error).__name__,
            error_message=_error_message(error),
        )

    def acquire_candidates(
        self,
        candidates: Sequence[ChessResultsCandidate],
    ) -> DiscoveryAcquisitionResult:
        """Process unique candidates without allowing one failure to abort the batch."""
        unique_candidates = self._unique_candidates(candidates)
        results: list[CandidateAcquisition] = []

        for candidate in unique_candidates:
            existing: dict[str, object] | None = None
            try:
                existing = self.registry.find_source_tournament(
                    candidate.provider,
                    candidate.external_id,
                )
                if (
                    existing is not None
                    and str(existing.get("status")) in _COMPLETED_STATUSES
                ):
                    results.append(self._existing_result(candidate, existing))
                    continue
                acquisition_result = self.acquisition.acquire(candidate.source_url)
            except Exception as exc:
                results.append(self._failed_result(candidate, exc, existing))
                continue
            results.append(self._acquired_result(candidate, acquisition_result))

        acquired = sum(result.action == "acquired" for result in results)
        skipped_existing = sum(
            result.action == "skipped_existing" for result in results
        )
        failed = sum(result.action == "failed" for result in results)
        return DiscoveryAcquisitionResult(
            discovered=len(unique_candidates),
            attempted=len(results),
            acquired=acquired,
            skipped_existing=skipped_existing,
            failed=failed,
            results=tuple(results),
        )


__all__ = [
    "CandidateAcquisition",
    "DiscoveryAcquisitionResult",
    "DiscoveryAcquisitionService",
]
