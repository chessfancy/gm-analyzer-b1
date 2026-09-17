"""Bridge discovered Chess-Results candidates into the accepted B2a service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable, Sequence

from chessgrandmaster.b2.acquisition import AcquisitionService
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.b2.sources.chess_results import PgnAvailability

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
    previous_raw_sha256: str | None = None
    source_file_id: int | None = None
    raw_object_key: str | None = None
    download_attempt_id: int | None = None

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
            "previous_raw_sha256": self.previous_raw_sha256,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "source_file_id": self.source_file_id,
            "raw_object_key": self.raw_object_key,
            "download_attempt_id": self.download_attempt_id,
        }


@dataclass(frozen=True)
class DiscoveryAcquisitionResult:
    """JSON-compatible summary for one discovery-to-acquisition batch."""

    discovered: int
    attempted: int
    acquired: int
    skipped_existing: int
    skipped_no_pgn: int
    failed: int
    results: tuple[CandidateAcquisition, ...]
    refreshed_unchanged: int = 0
    refreshed_changed: int = 0
    refresh_unavailable: int = 0

    @property
    def fetched(self) -> int:
        """Count raw-only fetch successes, including recent refreshes."""
        return sum(
            result.action
            in {"fetched", "refreshed_unchanged", "refreshed_changed"}
            for result in self.results
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "discovered": self.discovered,
            "attempted": self.attempted,
            "acquired": self.acquired,
            "skipped_existing": self.skipped_existing,
            "skipped_no_pgn": self.skipped_no_pgn,
            "failed": self.failed,
            "refreshed_unchanged": self.refreshed_unchanged,
            "refreshed_changed": self.refreshed_changed,
            "refresh_unavailable": self.refresh_unavailable,
            "results": [result.to_dict() for result in self.results],
        }


class DiscoveryAcquisitionService:
    """Idempotently route discovered candidates through B2a."""

    def __init__(
        self,
        registry: Registry,
        acquisition: AcquisitionService,
        pgn_probe: Callable[[ChessResultsCandidate], PgnAvailability],
    ) -> None:
        self.registry = registry
        self.acquisition = acquisition
        self.pgn_probe = pgn_probe

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
    def _refresh_eligible(
        candidate: ChessResultsCandidate,
        *,
        refresh_recent_days: int,
        as_of: date,
    ) -> bool:
        if refresh_recent_days <= 0 or candidate.to_date is None:
            return False
        try:
            end_date = date.fromisoformat(candidate.to_date)
        except ValueError:
            return False
        age = (as_of - end_date).days
        return 0 <= age <= refresh_recent_days

    @staticmethod
    def _raw_sha256(source_file: dict[str, object] | None) -> str | None:
        if source_file is None or source_file.get("sha256") is None:
            return None
        return str(source_file["sha256"])

    @staticmethod
    def _acquired_result(
        candidate: ChessResultsCandidate,
        acquisition_result: object,
        *,
        action: str = "acquired",
        previous_raw_sha256: str | None = None,
    ) -> CandidateAcquisition:
        status = getattr(acquisition_result, "tournament_status", None)
        return CandidateAcquisition(
            provider=candidate.provider,
            external_id=candidate.external_id,
            source_url=candidate.source_url,
            action=action,
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
            previous_raw_sha256=previous_raw_sha256,
            source_file_id=_optional_int(
                getattr(acquisition_result, "source_file_id", None)
            ),
            raw_object_key=(
                None
                if getattr(acquisition_result, "raw_object_key", None) is None
                else str(acquisition_result.raw_object_key)
            ),
            download_attempt_id=_optional_int(
                getattr(acquisition_result, "download_attempt_id", None)
            ),
        )

    @staticmethod
    def _failed_result(
        candidate: ChessResultsCandidate,
        error: BaseException,
        existing: dict[str, object] | None,
        previous_raw_sha256: str | None = None,
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
            previous_raw_sha256=previous_raw_sha256,
        )

    @staticmethod
    def _no_pgn_result(candidate: ChessResultsCandidate) -> CandidateAcquisition:
        return CandidateAcquisition(
            provider=candidate.provider,
            external_id=candidate.external_id,
            source_url=candidate.source_url,
            action="skipped_no_pgn",
            tournament_id=None,
            tournament_status=None,
            raw_sha256=None,
            revision_id=None,
        )

    @staticmethod
    def _refresh_unavailable_result(
        candidate: ChessResultsCandidate,
        existing: dict[str, object],
        previous_raw_sha256: str | None,
    ) -> CandidateAcquisition:
        return CandidateAcquisition(
            provider=candidate.provider,
            external_id=candidate.external_id,
            source_url=candidate.source_url,
            action="refresh_unavailable",
            tournament_id=_optional_int(existing.get("tournament_id")),
            tournament_status=(
                None
                if existing.get("status") is None
                else str(existing["status"])
            ),
            raw_sha256=None,
            revision_id=None,
            previous_raw_sha256=previous_raw_sha256,
        )

    def _process_candidates(
        self,
        candidates: Sequence[ChessResultsCandidate],
        *,
        refresh_recent_days: int = 0,
        as_of: date | None = None,
        fetch_only: bool = False,
    ) -> DiscoveryAcquisitionResult:
        """Process unique candidates without allowing one failure to abort the batch."""
        if isinstance(refresh_recent_days, bool) or not isinstance(
            refresh_recent_days, int
        ):
            raise TypeError("refresh_recent_days must be an integer")
        if refresh_recent_days < 0:
            raise ValueError("refresh_recent_days must not be negative")
        if as_of is not None and not isinstance(as_of, date):
            raise TypeError("as_of must be a date")
        refresh_as_of = as_of or date.today()
        unique_candidates = self._unique_candidates(candidates)
        results: list[CandidateAcquisition] = []
        completed_statuses = (
            _COMPLETED_STATUSES | {"DOWNLOADED", "VALIDATED"}
            if fetch_only
            else _COMPLETED_STATUSES
        )
        operation = (
            getattr(self.acquisition, "fetch", None)
            if fetch_only
            else self.acquisition.acquire
        )
        success_action = "fetched" if fetch_only else "acquired"

        for candidate in unique_candidates:
            existing: dict[str, object] | None = None
            previous_raw_sha256: str | None = None
            try:
                existing = self.registry.find_source_tournament(
                    candidate.provider,
                    candidate.external_id,
                )
                if (
                    existing is not None
                    and str(existing.get("status")) in completed_statuses
                ):
                    if (
                        str(existing.get("status")) == "CANONICALIZED"
                        and self._refresh_eligible(
                            candidate,
                            refresh_recent_days=refresh_recent_days,
                            as_of=refresh_as_of,
                        )
                    ):
                        latest_source_file = self.registry.find_latest_source_file(
                            candidate.provider,
                            candidate.external_id,
                        )
                        previous_raw_sha256 = self._raw_sha256(latest_source_file)
                        availability = self.pgn_probe(candidate)
                        available = getattr(availability, "available", None)
                        if not isinstance(available, bool):
                            raise TypeError(
                                "PGN availability probe returned an invalid result"
                            )
                        if not available:
                            results.append(
                                self._refresh_unavailable_result(
                                    candidate,
                                    existing,
                                    previous_raw_sha256,
                                )
                            )
                            continue
                        acquisition_result = operation(candidate.source_url)
                        raw_sha256 = getattr(acquisition_result, "raw_sha256", None)
                        raw_sha256 = (
                            None if raw_sha256 is None else str(raw_sha256)
                        )
                        action = (
                            "refreshed_unchanged"
                            if raw_sha256 == previous_raw_sha256
                            else "refreshed_changed"
                        )
                        results.append(
                            self._acquired_result(
                                candidate,
                                acquisition_result,
                                action=action,
                                previous_raw_sha256=previous_raw_sha256,
                            )
                        )
                        continue
                    results.append(self._existing_result(candidate, existing))
                    continue
                availability = self.pgn_probe(candidate)
                available = getattr(availability, "available", None)
                if not isinstance(available, bool):
                    raise TypeError("PGN availability probe returned an invalid result")
                if not available:
                    results.append(self._no_pgn_result(candidate))
                    continue
                acquisition_result = operation(candidate.source_url)
            except Exception as exc:
                results.append(
                    self._failed_result(
                        candidate,
                        exc,
                        existing,
                        previous_raw_sha256,
                    )
                )
                continue
            results.append(
                self._acquired_result(
                    candidate,
                    acquisition_result,
                    action=success_action,
                )
            )

        acquired = (
            sum(
                result.action
                in {"fetched", "refreshed_unchanged", "refreshed_changed"}
                for result in results
            )
            if fetch_only
            else sum(result.action == "acquired" for result in results)
        )
        skipped_existing = sum(
            result.action == "skipped_existing" for result in results
        )
        skipped_no_pgn = sum(
            result.action == "skipped_no_pgn" for result in results
        )
        failed = sum(result.action == "failed" for result in results)
        refreshed_unchanged = sum(
            result.action == "refreshed_unchanged" for result in results
        )
        refreshed_changed = sum(
            result.action == "refreshed_changed" for result in results
        )
        refresh_unavailable = sum(
            result.action == "refresh_unavailable" for result in results
        )
        return DiscoveryAcquisitionResult(
            discovered=len(unique_candidates),
            attempted=len(results),
            acquired=acquired,
            skipped_existing=skipped_existing,
            skipped_no_pgn=skipped_no_pgn,
            failed=failed,
            results=tuple(results),
            refreshed_unchanged=refreshed_unchanged,
            refreshed_changed=refreshed_changed,
            refresh_unavailable=refresh_unavailable,
        )

    def acquire_candidates(
        self,
        candidates: Sequence[ChessResultsCandidate],
        *,
        refresh_recent_days: int = 0,
        as_of: date | None = None,
    ) -> DiscoveryAcquisitionResult:
        """Process candidates through full B2a validation/canonicalization."""
        return self._process_candidates(
            candidates,
            refresh_recent_days=refresh_recent_days,
            as_of=as_of,
            fetch_only=False,
        )

    def fetch_candidates(
        self,
        candidates: Sequence[ChessResultsCandidate],
        *,
        refresh_recent_days: int = 0,
        as_of: date | None = None,
    ) -> DiscoveryAcquisitionResult:
        """Fetch raw PGNs and provenance without parsing or canonicalizing games."""
        return self._process_candidates(
            candidates,
            refresh_recent_days=refresh_recent_days,
            as_of=as_of,
            fetch_only=True,
        )


__all__ = [
    "CandidateAcquisition",
    "DiscoveryAcquisitionResult",
    "DiscoveryAcquisitionService",
]
