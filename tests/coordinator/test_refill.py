from __future__ import annotations

import hashlib
from pathlib import Path

from chessgrandmaster.b2.canonicalize import canonicalize_source_file
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.coordinator import Coordinator, JobState
from chessgrandmaster.coordinator.refill import refill_backfill_queue

PGN = '''[Event "Refill Fixture"]
[Result "*"]

1. e4 e5 2. Nf3 Nc6 *
'''


def seed_tournament(registry: Registry, root: Path, slug: str, source_name: str) -> int:
    tournament_id = registry.upsert_tournament(slug, name=slug, status="CANONICALIZED")
    source_id = registry.upsert_source(source_name, f"https://{source_name}.invalid")
    st_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id=slug,
        source_url=f"https://{source_name}.invalid/{slug}",
        pgn_url=f"https://{source_name}.invalid/{slug}.pgn",
    )
    raw = root / f"{slug}.pgn"
    raw.write_text(PGN, encoding="utf-8")
    data = raw.read_bytes()
    sf_id = registry.record_source_file(
        source_tournament_id=st_id,
        object_key=f"raw/{slug}.pgn",
        filename=raw.name,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
        content_type="application/x-chess-pgn",
    )
    canonicalize_source_file(
        registry,
        tournament_id,
        sf_id,
        raw,
        root / f"{slug}-canonical.pgn",
    )
    return tournament_id


def make_system(tmp_path: Path):
    registry = Registry(tmp_path / "registry.sqlite")
    coordinator = Coordinator(tmp_path / "coordinator.sqlite", archive_root=tmp_path / "archive")
    return registry, coordinator


def test_refill_selects_only_requested_source_and_registers_jobs(tmp_path: Path):
    registry, coordinator = make_system(tmp_path)
    seed_tournament(registry, tmp_path, "other-first", "other")
    wanted_id = seed_tournament(registry, tmp_path, "wanted-second", "chess-results")

    summary = refill_backfill_queue(
        registry=registry,
        coordinator=coordinator,
        package_root=tmp_path / "packages",
        source_name="chess-results",
        low_watermark=1,
        high_watermark=1,
        target_plies=1800,
        priority=500,
    )

    assert summary.registered_jobs == 1
    assert summary.packaged_tournaments == (wanted_id,)
    job = coordinator.list_jobs()[0]
    assert job.priority == 500
    assert job.state is JobState.PENDING
    assert job.input_identity["tournament_id"] == "wanted-second"


def test_refill_is_noop_when_eligible_queue_is_at_low_watermark(tmp_path: Path):
    registry, coordinator = make_system(tmp_path)
    seed_tournament(registry, tmp_path, "first", "chess-results")
    refill_backfill_queue(
        registry=registry, coordinator=coordinator, package_root=tmp_path / "packages",
        low_watermark=1, high_watermark=1, target_plies=1800, priority=500,
    )

    second = refill_backfill_queue(
        registry=registry, coordinator=coordinator, package_root=tmp_path / "packages",
        low_watermark=1, high_watermark=3, target_plies=1800, priority=500,
    )

    assert second.registered_jobs == 0
    assert second.reason == "queue_sufficient"
    assert len(coordinator.list_jobs()) == 1


def test_refill_skips_revision_already_registered_when_more_work_is_needed(tmp_path: Path):
    registry, coordinator = make_system(tmp_path)
    first_id = seed_tournament(registry, tmp_path, "first", "chess-results")
    second_id = seed_tournament(registry, tmp_path, "second", "chess-results")
    first = refill_backfill_queue(
        registry=registry, coordinator=coordinator, package_root=tmp_path / "packages",
        low_watermark=1, high_watermark=1, target_plies=1800, priority=500,
    )
    assert first.packaged_tournaments == (first_id,)

    # Drop the first job below the eligible threshold without changing its identity.
    with coordinator._connection(write=True) as connection:
        connection.execute("UPDATE jobs SET priority=100 WHERE job_id=?", (coordinator.list_jobs()[0].job_id,))

    second = refill_backfill_queue(
        registry=registry, coordinator=coordinator, package_root=tmp_path / "packages",
        low_watermark=1, high_watermark=1, target_plies=1800, priority=500,
    )

    assert second.packaged_tournaments == (second_id,)
    assert len(coordinator.list_jobs()) == 2


def test_refill_can_fill_multiple_jobs_to_high_watermark(tmp_path: Path):
    registry, coordinator = make_system(tmp_path)
    for idx in range(4):
        seed_tournament(registry, tmp_path, f"t-{idx}", "chess-results")

    summary = refill_backfill_queue(
        registry=registry, coordinator=coordinator, package_root=tmp_path / "packages",
        low_watermark=1, high_watermark=3, target_plies=1800, priority=500,
    )

    eligible = [job for job in coordinator.list_jobs() if job.priority >= 500]
    assert len(eligible) >= 3
    assert summary.eligible_after >= 3
    assert summary.registered_jobs >= 3
