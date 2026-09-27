"""Registry-to-coordinator queue refill for bulk backfill."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chessgrandmaster.b2.packaging import PackageService
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.b2.storage import LocalObjectStore

from .models import JobState
from .queue import Coordinator


@dataclass(frozen=True)
class RefillSummary:
    reason: str
    eligible_before: int
    eligible_after: int
    registered_jobs: int
    packaged_tournaments: tuple[int, ...]


def _eligible_count(coordinator: Coordinator, min_priority: int) -> int:
    return sum(
        1
        for job in coordinator.list_jobs()
        if job.state in (JobState.PENDING, JobState.RETRY_PENDING)
        and job.priority >= min_priority
    )


def _registered_revisions(coordinator: Coordinator) -> set[tuple[str, int]]:
    revisions: set[tuple[str, int]] = set()
    for job in coordinator.list_jobs():
        identity = job.input_identity
        tournament_id = identity.get("tournament_id")
        revision = identity.get("tournament_revision")
        if tournament_id is None or revision is None:
            continue
        revisions.add((str(tournament_id), int(revision)))
    return revisions


def _matches_source(registry: Registry, tournament_id: int, source_name: str | None) -> bool:
    if source_name is None:
        return True
    normalized = source_name.strip().lower()
    return any(
        str(item["source_name"]).strip().lower() == normalized
        for item in registry.list_source_tournaments_for_tournament(tournament_id)
    )


def refill_backfill_queue(
    *,
    registry: Registry,
    coordinator: Coordinator,
    package_root: str | Path,
    source_name: str | None = "chess-results",
    low_watermark: int = 8,
    high_watermark: int = 24,
    target_plies: int = 1800,
    priority: int = 500,
) -> RefillSummary:
    """Top up eligible bulk work from canonical registry tournaments."""

    if low_watermark < 0 or high_watermark < 1 or high_watermark < low_watermark:
        raise ValueError("queue watermarks must satisfy 0 <= low <= high")
    if target_plies < 1:
        raise ValueError("target_plies must be positive")

    eligible_before = _eligible_count(coordinator, priority)
    if eligible_before >= low_watermark:
        return RefillSummary(
            reason="queue_sufficient",
            eligible_before=eligible_before,
            eligible_after=eligible_before,
            registered_jobs=0,
            packaged_tournaments=(),
        )

    root = Path(package_root).expanduser().resolve()
    store = LocalObjectStore(root)
    workspace = root.parent / f".{root.name}.cgm-package-work"
    service = PackageService(registry, store, workspace)
    represented = _registered_revisions(coordinator)
    registered_jobs = 0
    packaged: list[int] = []
    eligible = eligible_before

    for tournament in registry.list_tournaments():
        if eligible >= high_watermark:
            break
        if tournament["status"] not in {"CANONICALIZED", "READY"}:
            continue
        tournament_id = int(tournament["id"])
        if not _matches_source(registry, tournament_id, source_name):
            continue
        revisions = registry.list_revisions_for_tournament(tournament_id)
        if not revisions:
            continue
        revision = int(revisions[-1]["revision_number"])
        identity = (str(tournament["slug"]), revision)
        if identity in represented:
            continue

        package = service.package(tournament_id, revision_number=revision, target_plies=target_plies)
        created = 0
        for shard in package.shards:
            coordinator.register_job(shard.job_path.parent, priority=priority)
            created += 1
        if created:
            represented.add(identity)
            packaged.append(tournament_id)
            registered_jobs += created
            eligible += created

    return RefillSummary(
        reason="refilled" if registered_jobs else "no_candidates",
        eligible_before=eligible_before,
        eligible_after=_eligible_count(coordinator, priority),
        registered_jobs=registered_jobs,
        packaged_tournaments=tuple(packaged),
    )
