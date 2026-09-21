from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chessgrandmaster.coordinator import (
    BundleValidationError,
    Coordinator,
    FilesystemBundleTransport,
    JobState,
    JobConflictError,
    LocalWorker,
    PROFILE_REGISTRY,
    ProviderProfile,
    write_checksums,
)


def make_job_bundle(root: Path, job_id: str = "job-a", *, config_hash: str = "cfg-a") -> Path:
    root.mkdir(parents=True)
    input_bytes = b"[Event \"fixture\"]\n\n1. e4 e5 *\n"
    (root / "input.pgn").write_bytes(input_bytes)
    job = {
        "schema_version": "cgm-job-1",
        "job_id": job_id,
        "tournament_id": "tournament-a",
        "tournament_revision": 1,
        "shard_index": 0,
        "input": {
            "key": f"tournaments/tournament-a/revisions/0001/shards/0000/input.pgn",
            "sha256": __import__("hashlib").sha256(input_bytes).hexdigest(),
            "games": 1,
            "plies": 2,
            "canonical_game_fingerprints": ["fingerprint-a"],
        },
        "analysis": {"pipeline_version": 3, "engine": "stockfish19", "depth": 19},
        "config_hash": config_hash,
    }
    manifest = {
        "schema_version": "cgm-tournament-1",
        "tournament_id": "tournament-a",
        "revision": 1,
        "shards": [{"job_id": job_id, "shard_index": 0}],
    }
    (root / "job.json").write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    write_checksums(root)
    return root


def make_result_bundle(
    root: Path,
    job_id: str = "job-a",
    *,
    config_hash: str = "cfg-a",
    input_sha256: str | None = None,
) -> Path:
    root.mkdir(parents=True)
    input_sha256 = input_sha256 or __import__("hashlib").sha256(
        b"[Event \"fixture\"]\n\n1. e4 e5 *\n"
    ).hexdigest()
    result = {
        "schema_version": "cgm-job-result-1",
        "job_id": job_id,
        "config_hash": config_hash,
        "input": {
            "sha256": input_sha256,
            "key": "tournaments/tournament-a/revisions/0001/shards/0000/input.pgn",
        },
        "shard_index": 0,
    }
    (root / "job-result.json").write_text(
        json.dumps(result, sort_keys=True), encoding="utf-8"
    )
    (root / "analysis.sqlite").write_bytes(b"SQLite fixture")
    (root / "Mistakes.pgn").write_text("", encoding="utf-8")
    (root / "Blunders.pgn").write_text("", encoding="utf-8")
    (root / "raw-uci").mkdir()
    (root / "raw-uci" / "game-0001.uci").write_text("bestmove e2e4\n", encoding="utf-8")
    write_checksums(root)
    return root


def make_coordinator(tmp_path: Path) -> Coordinator:
    return Coordinator(
        tmp_path / "coordinator.sqlite",
        archive_root=tmp_path / "archive",
        default_lease_seconds=300,
    )


def test_durable_state_machine_reaches_completed(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    job_bundle = make_job_bundle(tmp_path / "job")

    registered = coordinator.register_job(job_bundle)
    assert registered == "job-a"
    assert coordinator.get_job("job-a").state is JobState.PENDING

    lease = coordinator.lease_next("worker-1", "kaggle")
    assert lease is not None
    assert lease.job_id == "job-a"
    assert coordinator.get_job("job-a").state is JobState.LEASED

    exported = coordinator.export_job("job-a", tmp_path / "exported")
    assert exported == tmp_path / "exported"
    assert (exported / "job.json").is_file()
    assert coordinator.get_job("job-a").state is JobState.EXPORTED

    coordinator.mark_running("job-a")
    assert coordinator.get_job("job-a").state is JobState.RUNNING

    result = make_result_bundle(tmp_path / "result")
    receipt = coordinator.import_result(result)
    assert receipt.job_id == "job-a"
    assert coordinator.get_job("job-a").state is JobState.COMPLETED
    assert coordinator.get_attempts("job-a")[0].state is JobState.COMPLETED
    assert Path(receipt.archive_path, "job-result.json").is_file()
    assert Path(receipt.archive_path, "metadata.json").is_file()


def test_duplicate_registration_is_idempotent(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    original = make_job_bundle(tmp_path / "job-original")
    duplicate = tmp_path / "job-duplicate"
    __import__("shutil").copytree(original, duplicate)

    first = coordinator.register_job(original)
    second = coordinator.register_job(duplicate)

    assert first == second == "job-a"
    assert len(coordinator.list_jobs()) == 1


def test_registration_rejects_immutable_job_conflict(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    original = make_job_bundle(tmp_path / "job-original")
    changed = make_job_bundle(tmp_path / "job-changed", config_hash="different")
    coordinator.register_job(original)

    with pytest.raises(JobConflictError):
        coordinator.register_job(changed)


def test_sqlite_state_reopens_without_losing_queue_or_attempts(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    make_job_bundle(tmp_path / "job")
    coordinator.register_job(tmp_path / "job")
    coordinator.lease_next("worker-1", "kaggle")

    reopened = Coordinator(
        tmp_path / "coordinator.sqlite",
        archive_root=tmp_path / "archive",
        default_lease_seconds=300,
    )

    assert reopened.get_job("job-a").state is JobState.LEASED
    assert len(reopened.get_attempts("job-a")) == 1


def test_filesystem_transport_validates_and_copies_job_bundle(tmp_path: Path):
    job = make_job_bundle(tmp_path / "job")
    transport = FilesystemBundleTransport()

    exported = transport.export_job(job, tmp_path / "transport-export")

    assert (exported / "job.json").read_bytes() == (job / "job.json").read_bytes()



def test_lease_order_is_deterministic_by_priority_then_job_id(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    make_job_bundle(tmp_path / "job-z", "job-z")
    make_job_bundle(tmp_path / "job-a", "job-a")
    coordinator.register_job(tmp_path / "job-z")
    coordinator.register_job(tmp_path / "job-a")

    first = coordinator.lease_next("worker-1", "kaggle")
    second = coordinator.lease_next("worker-2", "kaggle")

    assert first is not None and second is not None
    assert [first.job_id, second.job_id] == ["job-a", "job-z"]


def test_expired_lease_becomes_stale_and_retry_pending(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    make_job_bundle(tmp_path / "job")
    coordinator.register_job(tmp_path / "job")
    coordinator.lease_next("worker-1", "deepnote", lease_seconds=-1)

    recovered = coordinator.recover_stale_attempts()

    assert recovered == 1
    assert coordinator.get_job("job-a").state is JobState.RETRY_PENDING
    assert coordinator.get_attempts("job-a")[0].state is JobState.STALE

    retry = coordinator.lease_next("worker-2", "deepnote")
    assert retry is not None
    assert retry.attempt_number == 2
    assert coordinator.get_job("job-a").state is JobState.LEASED


def test_result_checksum_failure_is_rejected_and_preserved(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    make_job_bundle(tmp_path / "job")
    coordinator.register_job(tmp_path / "job")
    coordinator.lease_next("worker-1", "kaggle")
    coordinator.export_job("job-a", tmp_path / "exported")

    result = make_result_bundle(tmp_path / "bad-result")
    (result / "analysis.sqlite").write_bytes(b"tampered")

    with pytest.raises(BundleValidationError):
        coordinator.import_result(result)

    assert coordinator.get_job("job-a").state is JobState.EXPORTED
    rejected = list((tmp_path / "archive" / "rejected").iterdir())
    assert rejected
    assert (rejected[0] / "analysis.sqlite").read_bytes() == b"tampered"


def test_result_identity_mismatch_never_completes_job(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    make_job_bundle(tmp_path / "job")
    coordinator.register_job(tmp_path / "job")
    coordinator.lease_next("worker-1", "kaggle")
    coordinator.export_job("job-a", tmp_path / "exported")

    result = make_result_bundle(tmp_path / "wrong-result", config_hash="wrong")
    with pytest.raises(BundleValidationError):
        coordinator.import_result(result)

    assert coordinator.get_job("job-a").state is JobState.EXPORTED
    assert coordinator.get_attempts("job-a")[0].state is JobState.EXPORTED
    assert not list((tmp_path / "archive" / "accepted").rglob("metadata.json"))

def test_result_input_identity_mismatch_is_rejected(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    make_job_bundle(tmp_path / "job")
    coordinator.register_job(tmp_path / "job")
    coordinator.lease_next("worker-1", "kaggle")
    coordinator.export_job("job-a", tmp_path / "exported")

    result = make_result_bundle(tmp_path / "wrong-input", input_sha256="0" * 64)
    with pytest.raises(BundleValidationError):
        coordinator.import_result(result)

    assert coordinator.get_job("job-a").state is JobState.EXPORTED
    assert list((tmp_path / "archive" / "rejected").iterdir())


def test_profiles_stay_outside_job_spec_and_oracle_is_secondary(tmp_path: Path):
    expected = {
        "kaggle": (4, 1, 1536),
        "deepnote": (2, 1, 768),
        "molab-marimo": (4, 1, 1536),
        "codespaces": (2, 1, 1024),
        "oracle-urgent": (2, 1, 1536),
    }
    for name, resources in expected.items():
        profile = PROFILE_REGISTRY[name]
        assert (profile.workers, profile.threads, profile.hash_mb) == resources
        assert isinstance(profile, ProviderProfile)
    assert PROFILE_REGISTRY["oracle-urgent"].role == "secondary-urgent"
    assert PROFILE_REGISTRY["oracle-urgent"].max_concurrent_jobs == 1

    job = make_job_bundle(tmp_path / "job")
    job_payload = json.loads((job / "job.json").read_text(encoding="utf-8"))
    forbidden = {"provider", "workers", "threads", "hash_mb", "hash_mb", "binary"}
    assert forbidden.isdisjoint(job_payload)
    assert "oracle-urgent" not in (job / "job.json").read_text(encoding="utf-8")


def test_oracle_profile_is_one_job_at_a_time(tmp_path: Path):
    coordinator = make_coordinator(tmp_path)
    make_job_bundle(tmp_path / "job-a", "job-a")
    make_job_bundle(tmp_path / "job-b", "job-b")
    coordinator.register_job(tmp_path / "job-a")
    coordinator.register_job(tmp_path / "job-b")

    first = coordinator.lease_next("oracle", "oracle-urgent")
    second = coordinator.lease_next("oracle", "oracle-urgent")

    assert first is not None
    assert second is None


def test_local_worker_materializes_without_invoking_provider(tmp_path: Path):
    job = make_job_bundle(tmp_path / "job")
    worker = LocalWorker("oracle-urgent", work_root=tmp_path / "worker")

    command = worker.prepare(job)

    assert command.profile == "oracle-urgent"
    assert command.workdir.is_dir()
    assert (command.workdir / "input.pgn").read_bytes() == (job / "input.pgn").read_bytes()
    assert "--workers" in command.command
    assert "2" in command.command
    assert "--hash-mb" in command.command
    assert "1536" in command.command
    assert command.invoked is False


def test_accepted_result_archive_does_not_write_canonical_registry(tmp_path: Path):
    canonical = tmp_path / "canonical.sqlite"
    with sqlite3.connect(canonical) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES ('unchanged')")

    coordinator = make_coordinator(tmp_path)
    make_job_bundle(tmp_path / "job")
    coordinator.register_job(tmp_path / "job")
    coordinator.lease_next("worker-1", "codespaces")
    coordinator.export_job("job-a", tmp_path / "exported")
    coordinator.import_result(make_result_bundle(tmp_path / "result"))

    with sqlite3.connect(canonical) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("unchanged",)
    assert not coordinator.canonical_registry_path
