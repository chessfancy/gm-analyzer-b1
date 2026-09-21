"""Durable local SQLite coordinator and filesystem dispatch facade."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .bundles import (
    BundleValidationError,
    FilesystemBundleTransport,
    canonical_json_bytes,
    validate_job_bundle,
    validate_result_bundle,
)
from .models import (
    AttemptRecord,
    AttemptState,
    CoordinatorError,
    CoordinatorStatus,
    ImportReceipt,
    InvalidTransitionError,
    JobConflictError,
    JobHandle,
    JobRecord,
    JobState,
    Lease,
    StatusSnapshot,
)
from .profiles import ProviderProfile, get_provider_profile


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    bundle_path TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    attempt_number INTEGER NOT NULL,
    provider TEXT NOT NULL,
    worker TEXT NOT NULL,
    state TEXT NOT NULL,
    lease_token TEXT NOT NULL UNIQUE,
    leased_at TEXT,
    lease_expires_at TEXT,
    exported_at TEXT,
    started_at TEXT,
    result_received_at TEXT,
    verified_at TEXT,
    completed_at TEXT,
    finished_at TEXT,
    export_path TEXT,
    result_path TEXT,
    archive_path TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_jobs_dispatch ON jobs(state, priority DESC, job_id);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts(job_id, attempt_number DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_provider_state ON attempts(provider, state);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _json_identity(value: dict[str, Any]) -> dict[str, Any]:
    copied = dict(value)
    fingerprints = copied.get("canonical_game_fingerprints")
    if isinstance(fingerprints, list):
        copied["canonical_game_fingerprints"] = tuple(fingerprints)
    return copied


def _safe_component(value: str) -> str:
    if _SAFE_COMPONENT.fullmatch(value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _job_id(value: str | JobHandle | JobRecord | Lease) -> str:
    if isinstance(value, (JobRecord, Lease)):
        return value.job_id
    return str(value)


def _copy_directory(source: Path, destination: Path) -> Path:
    source = source.resolve()
    destination = Path(destination).resolve()
    if source == destination:
        return destination
    if destination.exists() and not destination.is_dir():
        raise CoordinatorError(f"destination is not a directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=str(destination.parent)))
    staged_bundle = staging / "bundle"
    try:
        shutil.copytree(source, staged_bundle)
        if destination.exists():
            shutil.rmtree(destination)
        staged_bundle.replace(destination)
        return destination
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class Coordinator:
    """Oracle-local durable queue with a filesystem bundle transport.

    The coordinator stores only queue/attempt/result metadata.  It never opens,
    updates, or imports a canonical B2a/B1 registry.
    """

    def __init__(
        self,
        db_path: str | Path,
        archive_root: str | Path | None = None,
        *,
        default_lease_seconds: int | float = 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_root = (
            Path(archive_root).resolve()
            if archive_root is not None
            else self.db_path.parent / "archive"
        )
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.registered_root = self.archive_root / "registered-jobs"
        self.accepted_root = self.archive_root / "accepted"
        self.rejected_root = self.archive_root / "rejected"
        self.default_lease_seconds = default_lease_seconds
        self._clock = clock or _utc_now
        # Deliberately not configurable through a canonical registry object.
        self.canonical_registry_path: None = None
        self._initialize()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            connection.executescript(_SCHEMA)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _get_job_row(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise CoordinatorError(f"unknown job: {job_id}")
        return row

    @staticmethod
    def _get_current_attempt_row(
        connection: sqlite3.Connection, job_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM attempts WHERE job_id = ? ORDER BY attempt_number DESC LIMIT 1",
            (job_id,),
        ).fetchone()

    @staticmethod
    def _job_record(row: sqlite3.Row) -> JobRecord:
        identity = _json_identity(json.loads(row["identity_json"]))
        return JobRecord(
            job_id=row["job_id"],
            state=JobState(row["state"]),
            config_hash=row["config_hash"],
            input_sha256=row["input_sha256"],
            input_identity=identity,
            priority=row["priority"],
            bundle_path=Path(row["bundle_path"]),
            registered_at=row["registered_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _attempt_record(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            attempt_id=row["attempt_id"],
            job_id=row["job_id"],
            attempt_number=row["attempt_number"],
            provider=row["provider"],
            worker=row["worker"],
            state=AttemptState(row["state"]),
            lease_token=row["lease_token"],
            leased_at=row["leased_at"],
            lease_expires_at=row["lease_expires_at"],
            export_path=Path(row["export_path"]) if row["export_path"] else None,
            result_path=Path(row["result_path"]) if row["result_path"] else None,
            archive_path=Path(row["archive_path"]) if row["archive_path"] else None,
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )

    def register_job(self, bundle_path: str | Path, priority: int = 0) -> JobHandle:
        """Validate and idempotently register one immutable B2b job bundle."""

        validated = validate_job_bundle(bundle_path)
        canonical_job = canonical_json_bytes(validated.job_json).decode("utf-8")
        canonical_manifest = canonical_json_bytes(validated.manifest_json).decode("utf-8")
        identity_json = canonical_json_bytes(validated.input_identity).decode("utf-8")
        now = _as_iso(self._now())
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (validated.job_id,)
            ).fetchone()
            if existing is not None:
                same = (
                    existing["job_json"] == canonical_job
                    and existing["manifest_json"] == canonical_manifest
                    and existing["input_sha256"] == validated.input_identity["input_sha256"]
                    and existing["config_hash"] == validated.config_hash
                    and existing["identity_json"] == identity_json
                )
                if not same:
                    raise JobConflictError(
                        f"job id {validated.job_id!r} is already registered with different data"
                    )
                return JobHandle(validated.job_id)

            registered_path = self.registered_root / _safe_component(validated.job_id)
            _copy_directory(validated.path, registered_path)
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, job_json, manifest_json, input_sha256, config_hash,
                    identity_json, bundle_path, priority, state, registered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validated.job_id,
                    canonical_job,
                    canonical_manifest,
                    validated.input_identity["input_sha256"],
                    validated.config_hash,
                    identity_json,
                    str(registered_path),
                    int(priority),
                    JobState.PENDING.value,
                    now,
                    now,
                ),
            )
        return JobHandle(validated.job_id)

    def get_job(self, job_id: str | JobHandle | JobRecord) -> JobRecord:
        job_key = _job_id(job_id)
        with self._connection() as connection:
            return self._job_record(self._get_job_row(connection, job_key))

    def list_jobs(self, state: JobState | str | None = None) -> tuple[JobRecord, ...]:
        state_value = state.value if isinstance(state, JobState) else state
        with self._connection() as connection:
            if state_value is None:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY priority DESC, job_id ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE state = ? ORDER BY priority DESC, job_id ASC",
                    (str(state_value),),
                ).fetchall()
            return tuple(self._job_record(row) for row in rows)

    def get_attempts(self, job_id: str | JobHandle | JobRecord) -> tuple[AttemptRecord, ...]:
        job_key = _job_id(job_id)
        with self._connection() as connection:
            self._get_job_row(connection, job_key)
            rows = connection.execute(
                "SELECT * FROM attempts WHERE job_id = ? ORDER BY attempt_number ASC",
                (job_key,),
            ).fetchall()
            return tuple(self._attempt_record(row) for row in rows)

    def get_attempt(self, attempt_id: int) -> AttemptRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (int(attempt_id),)
            ).fetchone()
            if row is None:
                raise CoordinatorError(f"unknown attempt: {attempt_id}")
            return self._attempt_record(row)

    def status(self, job_id: str | JobHandle | JobRecord) -> StatusSnapshot:
        job_key = _job_id(job_id)
        return StatusSnapshot(job=self.get_job(job_key), attempts=self.get_attempts(job_key))

    query_status = status
    get_status = status
    job_status = get_job
    attempt_status = get_attempt

    @staticmethod
    def _provider_limits(provider: str | ProviderProfile) -> tuple[str, int | None]:
        try:
            profile = get_provider_profile(provider)
        except ValueError:
            # The queue remains provider-neutral and permits a later adapter to
            # use a custom name; named profiles still receive their safeguards.
            return str(provider), None
        return profile.name, profile.max_concurrent_jobs or profile.workers

    def lease_next(
        self,
        worker: str,
        provider: str | ProviderProfile = "oracle-urgent",
        lease_seconds: int | float | None = None,
    ) -> Lease | None:
        """Lease the deterministically first pending job for a provider."""

        provider_name, max_jobs = self._provider_limits(provider)
        lease_duration = (
            self.default_lease_seconds if lease_seconds is None else lease_seconds
        )
        now_dt = self._now()
        now = _as_iso(now_dt)
        expires = _as_iso(now_dt + timedelta(seconds=float(lease_duration)))
        worker_name = str(worker)
        with self._connection(write=True) as connection:
            if max_jobs is not None:
                active = connection.execute(
                    """
                    SELECT COUNT(*) FROM attempts
                    WHERE provider = ? AND state IN ('LEASED', 'EXPORTED', 'RUNNING')
                    """,
                    (provider_name,),
                ).fetchone()[0]
                if active >= max_jobs:
                    return None
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state IN ('PENDING', 'RETRY_PENDING')
                ORDER BY priority DESC, job_id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            previous = connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) FROM attempts WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()[0]
            attempt_number = int(previous) + 1
            token = secrets.token_urlsafe(24)
            cursor = connection.execute(
                """
                INSERT INTO attempts (
                    job_id, attempt_number, provider, worker, state, lease_token,
                    leased_at, lease_expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["job_id"],
                    attempt_number,
                    provider_name,
                    worker_name,
                    AttemptState.LEASED.value,
                    token,
                    now,
                    expires,
                    now,
                ),
            )
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (JobState.LEASED.value, now, row["job_id"]),
            )
            attempt_id = int(cursor.lastrowid)
            return Lease(
                job_id=row["job_id"],
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                provider=provider_name,
                worker=worker_name,
                lease_token=token,
                leased_at=now,
                lease_expires_at=expires,
                bundle_path=Path(row["bundle_path"]),
            )

    def export_job(
        self,
        job_id: str | JobHandle | JobRecord,
        destination: str | Path,
    ) -> Path:
        """Re-verify and copy a registered job bundle to a transport directory."""

        job_key = _job_id(job_id)
        with self._connection() as connection:
            row = self._get_job_row(connection, job_key)
            attempt = self._get_current_attempt_row(connection, job_key)
        assert attempt is not None
        state = JobState(row["state"])
        if state in (JobState.EXPORTED, JobState.RUNNING):
            existing = Path(attempt["export_path"]) if attempt["export_path"] else None
            if existing is not None and existing.exists():
                return existing
        if state is not JobState.LEASED:
            raise InvalidTransitionError(f"cannot export job in state {state.value}")
        source = Path(row["bundle_path"])
        validated = validate_job_bundle(source)
        if validated.job_id != job_key:
            raise BundleValidationError("registered bundle job id changed", bundle_path=source)
        exported = _copy_directory(source, Path(destination))
        now = _as_iso(self._now())
        with self._connection(write=True) as connection:
            current = self._get_job_row(connection, job_key)
            current_attempt = self._get_current_attempt_row(connection, job_key)
            if JobState(current["state"]) is not JobState.LEASED or current_attempt is None:
                raise InvalidTransitionError("job lease changed while exporting")
            connection.execute(
                """
                UPDATE attempts SET state = ?, exported_at = ?, export_path = ?
                WHERE attempt_id = ?
                """,
                (AttemptState.EXPORTED.value, now, str(exported), current_attempt["attempt_id"]),
            )
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (JobState.EXPORTED.value, now, job_key),
            )
        return exported

    def mark_running(self, job_id: str | JobHandle | JobRecord) -> AttemptRecord:
        """Mark the current exported attempt as running without launching it."""

        job_key = _job_id(job_id)
        now = _as_iso(self._now())
        with self._connection(write=True) as connection:
            row = self._get_job_row(connection, job_key)
            attempt = self._get_current_attempt_row(connection, job_key)
            if attempt is None:
                raise InvalidTransitionError("job has no attempt")
            state = JobState(row["state"])
            if state is JobState.RUNNING:
                return self._attempt_record(attempt)
            if state is not JobState.EXPORTED:
                raise InvalidTransitionError(f"cannot mark {state.value} job as running")
            connection.execute(
                "UPDATE attempts SET state = ?, started_at = ? WHERE attempt_id = ?",
                (AttemptState.RUNNING.value, now, attempt["attempt_id"]),
            )
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (JobState.RUNNING.value, now, job_key),
            )
            updated = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt["attempt_id"],)
            ).fetchone()
            assert updated is not None
            return self._attempt_record(updated)

    start_attempt = mark_running

    def import_result(self, result_path: str | Path) -> ImportReceipt:
        """Verify, archive, and complete a result bundle for the active attempt."""

        result_root = Path(result_path)
        try:
            shape = validate_result_bundle(result_root)
        except BundleValidationError as exc:
            self._reject_result(result_root, exc)
            raise

        try:
            with self._connection() as connection:
                job_row = connection.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (shape.job_id,)
                ).fetchone()
            if job_row is None:
                raise BundleValidationError(
                    f"result references unknown job: {shape.job_id}",
                    bundle_path=result_root,
                    job_id=shape.job_id,
                )
            expected = validate_job_bundle(Path(job_row["bundle_path"]))
            verified = validate_result_bundle(result_root, expected_job=expected)
        except BundleValidationError as exc:
            self._reject_result(result_root, exc)
            raise

        with self._connection() as connection:
            current = self._get_current_attempt_row(connection, shape.job_id)
            state = JobState(job_row["state"])
            if state is JobState.COMPLETED and current is not None and current["archive_path"]:
                return ImportReceipt(
                    job_id=shape.job_id,
                    attempt_id=current["attempt_id"],
                    attempt_number=current["attempt_number"],
                    archive_path=Path(current["archive_path"]),
                )
            if current is None or state not in (
                JobState.EXPORTED,
                JobState.RUNNING,
                JobState.RESULT_RECEIVED,
                JobState.VERIFIED,
            ):
                exc = BundleValidationError(
                    f"job {shape.job_id} is not awaiting a result (state={state.value})",
                    bundle_path=result_root,
                    job_id=shape.job_id,
                )
                self._reject_result(result_root, exc)
                raise exc
            attempt_id = current["attempt_id"]
            attempt_number = current["attempt_number"]

        archive_path = (
            self.accepted_root
            / _safe_component(shape.job_id)
            / f"attempt-{int(attempt_number):04d}"
        )
        try:
            _copy_directory(verified.path, archive_path)
            metadata = {
                "job_id": shape.job_id,
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "config_hash": verified.config_hash,
                "input_identity": verified.input_identity,
                "imported_at": _as_iso(self._now()),
                "source_path": str(result_root.resolve()),
            }
            (archive_path / "metadata.json").write_bytes(canonical_json_bytes(metadata))
        except Exception as exc:
            raise CoordinatorError(f"could not archive result bundle: {exc}") from exc

        now = _as_iso(self._now())
        with self._connection(write=True) as connection:
            current = self._get_current_attempt_row(connection, shape.job_id)
            row = self._get_job_row(connection, shape.job_id)
            if current is None or current["attempt_id"] != attempt_id:
                raise InvalidTransitionError("job attempt changed while importing result")
            state = JobState(row["state"])
            if state not in (JobState.EXPORTED, JobState.RUNNING):
                raise InvalidTransitionError(f"cannot accept result in state {state.value}")
            connection.execute(
                """
                UPDATE attempts SET state = ?, result_received_at = ?, result_path = ?,
                    verified_at = ?, completed_at = ?, finished_at = ?, archive_path = ?
                WHERE attempt_id = ?
                """,
                (
                    AttemptState.COMPLETED.value,
                    now,
                    str(result_root.resolve()),
                    now,
                    now,
                    now,
                    str(archive_path),
                    attempt_id,
                ),
            )
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (JobState.COMPLETED.value, now, shape.job_id),
            )
        return ImportReceipt(
            job_id=shape.job_id,
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            archive_path=archive_path,
        )

    def _reject_result(self, result_root: Path, error: BundleValidationError) -> Path:
        rejection = self.rejected_root / (
            f"{_safe_component(error.job_id or 'unknown')}-"
            f"{self._now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        )
        rejection.parent.mkdir(parents=True, exist_ok=True)
        if result_root.exists() and result_root.is_dir():
            _copy_directory(result_root, rejection)
        elif result_root.exists():
            rejection.mkdir(parents=True, exist_ok=True)
            shutil.copy2(result_root, rejection / result_root.name)
        else:
            rejection.mkdir(parents=True, exist_ok=True)
        reason = {
            "error": str(error),
            "job_id": error.job_id,
            "source_path": str(result_root),
            "rejected_at": _as_iso(self._now()),
        }
        (rejection / "rejection.json").write_bytes(canonical_json_bytes(reason))
        error.rejected_path = rejection
        return rejection

    def fail_attempt(
        self,
        job_id: str | JobHandle | JobRecord,
        reason: str,
        *,
        retry: bool = False,
    ) -> AttemptRecord:
        """Record a provider failure and optionally enqueue a retry."""

        job_key = _job_id(job_id)
        now = _as_iso(self._now())
        with self._connection(write=True) as connection:
            row = self._get_job_row(connection, job_key)
            attempt = self._get_current_attempt_row(connection, job_key)
            if attempt is None or JobState(row["state"]) in (
                JobState.COMPLETED,
                JobState.VERIFIED,
            ):
                raise InvalidTransitionError("job has no failing active attempt")
            connection.execute(
                """
                UPDATE attempts SET state = ?, failure_reason = ?, finished_at = ?
                WHERE attempt_id = ?
                """,
                (AttemptState.FAILED.value, str(reason), now, attempt["attempt_id"]),
            )
            new_state = JobState.RETRY_PENDING if retry else JobState.FAILED
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (new_state.value, now, job_key),
            )
            updated = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt["attempt_id"],)
            ).fetchone()
            assert updated is not None
            return self._attempt_record(updated)

    def retry_job(self, job_id: str | JobHandle | JobRecord) -> JobRecord:
        """Put a failed/stale job back into the deterministic dispatch queue."""

        job_key = _job_id(job_id)
        now = _as_iso(self._now())
        with self._connection(write=True) as connection:
            row = self._get_job_row(connection, job_key)
            state = JobState(row["state"])
            if state is JobState.COMPLETED:
                raise InvalidTransitionError("completed jobs cannot be retried")
            if state not in (
                JobState.FAILED,
                JobState.STALE,
                JobState.RETRY_PENDING,
                JobState.PENDING,
            ):
                raise InvalidTransitionError(f"cannot retry job in state {state.value}")
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (JobState.RETRY_PENDING.value, now, job_key),
            )
            return self._job_record(self._get_job_row(connection, job_key))

    retry = retry_job

    def recover_stale_attempts(self, now: datetime | None = None) -> int:
        """Mark expired leases stale and enqueue their jobs for another attempt."""

        current = now or self._now()
        now_text = _as_iso(current)
        recovered = 0
        with self._connection(write=True) as connection:
            rows = connection.execute(
                """
                SELECT a.attempt_id, a.job_id
                FROM attempts a JOIN jobs j ON j.job_id = a.job_id
                WHERE a.state IN ('LEASED', 'EXPORTED', 'RUNNING')
                  AND a.lease_expires_at IS NOT NULL
                  AND a.lease_expires_at <= ?
                ORDER BY a.attempt_id ASC
                """,
                (now_text,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE attempts SET state = ?, failure_reason = ?, finished_at = ?
                    WHERE attempt_id = ? AND state IN ('LEASED', 'EXPORTED', 'RUNNING')
                    """,
                    (
                        AttemptState.STALE.value,
                        "lease expired",
                        now_text,
                        row["attempt_id"],
                    ),
                )
                changed = connection.execute("SELECT changes()").fetchone()[0]
                if changed:
                    connection.execute(
                        "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                        (JobState.RETRY_PENDING.value, now_text, row["job_id"]),
                    )
                    recovered += 1
        return recovered

    recover_stale = recover_stale_attempts


# Names that make the queue boundary explicit to integrations.
CoordinatorQueue = Coordinator
DurableCoordinator = Coordinator
OracleCoordinator = Coordinator

__all__ = [
    "Coordinator",
    "CoordinatorQueue",
    "DurableCoordinator",
    "FilesystemBundleTransport",
    "OracleCoordinator",
]
