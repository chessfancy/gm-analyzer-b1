"""Public data models for the local Oracle coordinator."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class JobState(str, Enum):
    """Durable coordinator states for an immutable job identity."""

    PENDING = "PENDING"
    LEASED = "LEASED"
    EXPORTED = "EXPORTED"
    RUNNING = "RUNNING"
    RESULT_RECEIVED = "RESULT_RECEIVED"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STALE = "STALE"
    RETRY_PENDING = "RETRY_PENDING"


AttemptState = JobState


class CoordinatorError(RuntimeError):
    """Base exception for coordinator failures."""


class InvalidTransitionError(CoordinatorError):
    """Raised when a requested queue transition is not legal."""


class JobConflictError(CoordinatorError):
    """Raised when a job id is registered with different immutable data."""


class JobHandle(str):
    """String-compatible result returned by :meth:`Coordinator.register_job`."""

    def __new__(cls, job_id: str) -> "JobHandle":
        return str.__new__(cls, job_id)

    @property
    def job_id(self) -> str:
        return str(self)


@dataclass(frozen=True)
class JobRecord:
    """Inspectable immutable snapshot of a queue job row."""

    job_id: str
    state: JobState
    config_hash: str
    input_sha256: str
    input_identity: Mapping[str, Any] = field(default_factory=dict)
    priority: int = 0
    bundle_path: Path | None = None
    registered_at: str = ""
    updated_at: str = ""

    @property
    def status(self) -> JobState:
        return self.state

    def __str__(self) -> str:
        return self.job_id


@dataclass(frozen=True)
class AttemptRecord:
    """Inspectable snapshot of one lease/attempt row."""

    attempt_id: int
    job_id: str
    attempt_number: int
    provider: str
    worker: str
    state: AttemptState
    lease_token: str
    leased_at: str | None = None
    lease_expires_at: str | None = None
    export_path: Path | None = None
    result_path: Path | None = None
    archive_path: Path | None = None
    failure_reason: str | None = None
    created_at: str = ""
    finished_at: str | None = None

    @property
    def status(self) -> AttemptState:
        return self.state


@dataclass(frozen=True)
class Lease:
    """A newly created provider lease."""

    job_id: str
    attempt_id: int
    attempt_number: int
    provider: str
    worker: str
    lease_token: str
    leased_at: str
    lease_expires_at: str
    bundle_path: Path


@dataclass(frozen=True)
class ImportReceipt:
    """Result of accepting and archiving a verified result bundle."""

    job_id: str
    attempt_id: int
    attempt_number: int
    archive_path: Path
    state: JobState = JobState.COMPLETED


@dataclass(frozen=True)
class StatusSnapshot:
    """Combined job and attempt view for status/query callers."""

    job: JobRecord
    attempts: tuple[AttemptRecord, ...]


# A small alias is useful to callers that use "status" terminology.
CoordinatorStatus = StatusSnapshot
