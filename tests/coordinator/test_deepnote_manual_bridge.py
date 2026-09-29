from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from chessgrandmaster.coordinator import Coordinator, write_checksums
from chessgrandmaster.coordinator.deepnote_manual_bridge import (
    CURRENT_ARCHIVE,
    CURRENT_POINTER,
    batch_archive_path,
    READY_POINTER,
    DeepnoteManualBridge,
)
from chessgrandmaster.coordinator.manual_batch import create_manual_batch
from chessgrandmaster.coordinator.deepnote_manual_worker import run_deepnote_manual_cycle


class MemoryStorage:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.upload_log: list[str] = []

    def read_bytes(self, path: str) -> bytes | None:
        return self.files.get(path)

    def download(self, path: str, destination: Path) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[path])

    def upload(self, path: str, source: Path) -> None:
        self.files[path] = source.read_bytes()
        self.upload_log.append(path)

    def delete(self, path: str) -> None:
        self.files.pop(path, None)


def make_job_bundle(root: Path, job_id: str, ordinal: int) -> Path:
    root.mkdir(parents=True)
    input_bytes = f'[Event "fixture-{ordinal}"]\n\n1. e4 e5 *\n'.encode()
    (root / "input.pgn").write_bytes(input_bytes)
    job = {
        "schema_version": "cgm-job-1",
        "job_id": job_id,
        "tournament_id": f"tournament-{ordinal}",
        "tournament_revision": 1,
        "shard_index": 0,
        "input": {
            "key": f"tournaments/tournament-{ordinal}/revisions/0001/shards/0000/input.pgn",
            "sha256": hashlib.sha256(input_bytes).hexdigest(),
            "games": 1,
            "plies": 2,
            "canonical_game_fingerprints": [f"fingerprint-{ordinal}"],
        },
        "analysis": {"pipeline_version": 3, "engine": "stockfish19", "depth": 19},
        "config_hash": "cfg-a",
    }
    manifest = {
        "schema_version": "cgm-tournament-1",
        "tournament_id": f"tournament-{ordinal}",
        "revision": 1,
        "shards": [{"job_id": job_id, "shard_index": 0}],
    }
    (root / "job.json").write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    write_checksums(root)
    return root


def fake_executor(bundle: Path, result: Path, provider: str) -> Path:
    job = json.loads((bundle / "job.json").read_text())
    result.mkdir(parents=True)
    payload = {
        "schema_version": "cgm-job-result-1",
        "job_id": job["job_id"],
        "config_hash": job["config_hash"],
        "tournament_id": job["tournament_id"],
        "tournament_revision": job["tournament_revision"],
        "shard_index": job["shard_index"],
        "input": {
            "key": job["input"]["key"],
            "sha256": job["input"]["sha256"],
            "canonical_game_fingerprints": job["input"]["canonical_game_fingerprints"],
        },
    }
    (result / "job-result.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    (result / "analysis.sqlite").write_bytes(b"sqlite")
    (result / "Mistakes.pgn").write_text("", encoding="utf-8")
    (result / "Blunders.pgn").write_text("", encoding="utf-8")
    (result / "raw-uci").mkdir()
    (result / "raw-uci" / "worker.jsonl.gz").write_bytes(b"gzip")
    write_checksums(result)
    return result


def seed_coordinator(tmp_path: Path, jobs: int) -> Coordinator:
    coordinator = Coordinator(tmp_path / "coordinator.sqlite", archive_root=tmp_path / "archive")
    for index in range(jobs):
        bundle = make_job_bundle(tmp_path / f"seed-{index}", f"job-{index}", index)
        coordinator.register_job(bundle, priority=500)
    return coordinator


def _publish_local_tree(storage: MemoryStorage, work: Path, ready: dict) -> None:
    local_root = work / ready["result_root"]
    remote_root = str(ready["result_root"])
    for path in sorted(item for item in local_root.rglob("*") if item.is_file()):
        relative = path.relative_to(local_root).as_posix()
        storage.files[f"{remote_root}/{relative}"] = path.read_bytes()
    storage.files[READY_POINTER] = (work / READY_POINTER).read_bytes()


def test_worker_cycle_publishes_project_tree_pointer_without_result_zip(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 1)
    batch = create_manual_batch(
        coordinator=coordinator,
        destination=tmp_path / "batch",
        provider="deepnote",
        max_jobs=1,
        min_priority=500,
    )
    from chessgrandmaster.coordinator.manual_batch import archive_directory

    batch_zip = archive_directory(batch.path, tmp_path / "current-batch.zip")
    work = tmp_path / "deepnote-work"
    inbox = work / "cgm-manual/inbox"
    inbox.mkdir(parents=True)
    shutil.copy2(batch_zip, inbox / "current-batch.zip")
    digest = hashlib.sha256((inbox / "current-batch.zip").read_bytes()).hexdigest()
    (inbox / "current.json").write_text(json.dumps({
        "schema_version": "cgm-deepnote-current-1",
        "batch_id": batch.batch_id,
        "archive_path": "cgm-manual/inbox/current-batch.zip",
        "sha256": digest,
    }), encoding="utf-8")

    summary = run_deepnote_manual_cycle(work_root=work, executor=fake_executor)

    ready = json.loads((work / READY_POINTER).read_text())
    result_root = work / ready["result_root"]
    checksums = result_root / "checksums.json"
    assert summary.batch_id == batch.batch_id
    assert ready["schema_version"] == "cgm-deepnote-ready-2"
    assert ready["transport"] == "project-tree"
    assert ready["batch_id"] == batch.batch_id
    assert ready["result_root"] == f"cgm-manual/runtime/{batch.batch_id}/result"
    assert ready["checksums_path"] == f"{ready['result_root']}/checksums.json"
    assert ready["checksums_sha256"] == hashlib.sha256(checksums.read_bytes()).hexdigest()
    assert summary.result_root == result_root
    assert not list((work / "cgm-manual/outbox").glob("*.zip")) if (work / "cgm-manual/outbox").exists() else True


def test_deepnote_worker_writes_progress_heartbeat(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 2)
    batch = create_manual_batch(
        coordinator=coordinator, destination=tmp_path / "batch",
        provider="deepnote", max_jobs=2, min_priority=500,
    )
    from chessgrandmaster.coordinator.manual_batch import archive_directory

    batch_zip = archive_directory(batch.path, tmp_path / "current-batch.zip")
    work = tmp_path / "deepnote-work"
    inbox = work / "cgm-manual/inbox"
    inbox.mkdir(parents=True)
    shutil.copy2(batch_zip, inbox / "current-batch.zip")
    digest = hashlib.sha256((inbox / "current-batch.zip").read_bytes()).hexdigest()
    (inbox / "current.json").write_text(json.dumps({
        "schema_version": "cgm-deepnote-current-1",
        "batch_id": batch.batch_id,
        "archive_path": "cgm-manual/inbox/current-batch.zip",
        "sha256": digest,
    }), encoding="utf-8")

    run_deepnote_manual_cycle(work_root=work, executor=fake_executor)

    progress = json.loads((work / "cgm-manual/runtime" / batch.batch_id / "progress.json").read_text())
    assert progress["schema_version"] == "cgm-deepnote-progress-1"
    assert progress["batch_id"] == batch.batch_id
    assert progress["jobs_total"] == 2
    assert progress["jobs_completed"] == 2
    assert progress["state"] == "complete"
    assert progress["games_completed"] == 2
    assert progress["plies_completed"] == 4
    assert progress["updated_at"]


def test_oracle_bridge_imports_project_tree_and_publishes_next_batch(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 3)
    storage = MemoryStorage()
    bridge = DeepnoteManualBridge(
        coordinator=coordinator,
        storage=storage,
        root=tmp_path / "bridge",
        max_jobs=2,
        min_priority=500,
    )

    first = bridge.ensure_current_batch()
    assert first is not None
    assert first.jobs == 2
    assert storage.upload_log[-1] == CURRENT_POINTER

    deepnote = tmp_path / "deepnote"
    inbox = deepnote / "cgm-manual/inbox"
    inbox.mkdir(parents=True)
    pointer = json.loads(storage.files[CURRENT_POINTER])
    remote_archive = pointer["archive_path"]
    (deepnote / remote_archive).parent.mkdir(parents=True, exist_ok=True)
    (deepnote / remote_archive).write_bytes(storage.files[remote_archive])
    (inbox / "current.json").write_bytes(storage.files[CURRENT_POINTER])
    run_deepnote_manual_cycle(work_root=deepnote, executor=fake_executor)

    ready = json.loads((deepnote / READY_POINTER).read_text())
    _publish_local_tree(storage, deepnote, ready)

    processed = bridge.poll_once()

    assert processed is not None
    assert processed.completed == 2
    assert processed.rejected == 0
    assert coordinator.get_job("job-0").state.value == "COMPLETED"
    assert coordinator.get_job("job-1").state.value == "COMPLETED"
    current = json.loads(storage.files[CURRENT_POINTER])
    assert current["batch_id"] != first.batch_id
    assert current["jobs"] == 1
    assert READY_POINTER not in storage.files
    assert not any(path.startswith(ready["result_root"] + "/") for path in storage.files)


def test_oracle_bridge_recovers_completed_tree_without_ready_pointer(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 3)
    storage = MemoryStorage()
    bridge = DeepnoteManualBridge(
        coordinator=coordinator,
        storage=storage,
        root=tmp_path / "bridge",
        max_jobs=2,
        min_priority=500,
    )
    first = bridge.ensure_current_batch()
    assert first is not None

    deepnote = tmp_path / "deepnote"
    inbox = deepnote / "cgm-manual/inbox"
    inbox.mkdir(parents=True)
    pointer = json.loads(storage.files[CURRENT_POINTER])
    remote_archive = pointer["archive_path"]
    (deepnote / remote_archive).parent.mkdir(parents=True, exist_ok=True)
    (deepnote / remote_archive).write_bytes(storage.files[remote_archive])
    (inbox / "current.json").write_bytes(storage.files[CURRENT_POINTER])
    run_deepnote_manual_cycle(work_root=deepnote, executor=fake_executor)
    ready = json.loads((deepnote / READY_POINTER).read_text())
    _publish_local_tree(storage, deepnote, ready)
    storage.files.pop(READY_POINTER, None)

    processed = bridge.poll_once()

    assert processed is not None
    assert processed.batch_id == first.batch_id
    assert processed.completed == 2
    assert processed.rejected == 0
    assert coordinator.get_job("job-0").state.value == "COMPLETED"
    assert coordinator.get_job("job-1").state.value == "COMPLETED"
    current = json.loads(storage.files[CURRENT_POINTER])
    assert current["batch_id"] != first.batch_id
    assert current["jobs"] == 1



class FailCurrentPointerOnceStorage(MemoryStorage):
    def __init__(self):
        super().__init__()
        self.fail_current_pointer_once = True

    def upload(self, path: str, source: Path) -> None:
        if path == CURRENT_POINTER and self.fail_current_pointer_once:
            self.fail_current_pointer_once = False
            raise RuntimeError("simulated current pointer publish failure")
        super().upload(path, source)


def test_batch_publish_uses_immutable_archive_and_pointer_last(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 1)
    storage = MemoryStorage()
    bridge = DeepnoteManualBridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "bridge",
        max_jobs=1, min_priority=500,
    )

    current = bridge.ensure_current_batch()

    assert current is not None
    pointer = json.loads(storage.files[CURRENT_POINTER])
    expected_archive = batch_archive_path(current.batch_id)
    assert pointer["archive_path"] == expected_archive
    assert expected_archive in storage.files
    assert CURRENT_ARCHIVE not in storage.files
    assert storage.upload_log[-1] == CURRENT_POINTER


def test_pending_publish_retry_does_not_reupload_large_archive(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 1)
    storage = FailCurrentPointerOnceStorage()
    bridge = DeepnoteManualBridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "bridge",
        max_jobs=1, min_priority=500,
    )

    import pytest
    with pytest.raises(RuntimeError, match="pointer publish failure"):
        bridge.ensure_current_batch()

    state = json.loads((tmp_path / "bridge/state.json").read_text())
    pending = state["pending_publish"]
    remote_archive = pending["remote_archive"]
    assert pending["archive_uploaded"] is True
    assert storage.upload_log.count(remote_archive) == 1

    current = bridge.ensure_current_batch()

    assert current is not None
    assert storage.upload_log.count(remote_archive) == 1
    assert storage.upload_log[-1] == CURRENT_POINTER


def test_legacy_pending_publish_state_migrates_to_immutable_archive(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 1)
    storage = FailCurrentPointerOnceStorage()
    bridge = DeepnoteManualBridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "bridge",
        max_jobs=1, min_priority=500,
    )
    import pytest
    with pytest.raises(RuntimeError):
        bridge.ensure_current_batch()

    state_path = tmp_path / "bridge/state.json"
    state = json.loads(state_path.read_text())
    pending = state["pending_publish"]
    batch_id = pending["batch_id"]
    # Recreate the shape written by the pre-immutable bridge.
    for key in ("remote_archive", "remote_sha", "archive_uploaded", "sha_uploaded"):
        pending.pop(key, None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    storage.files.clear()
    storage.upload_log.clear()

    current = bridge.ensure_current_batch()

    assert current is not None
    assert current.batch_id == batch_id
    pointer = json.loads(storage.files[CURRENT_POINTER])
    assert pointer["archive_path"] == batch_archive_path(batch_id)
    assert batch_archive_path(batch_id) in storage.files
    assert storage.upload_log[-1] == CURRENT_POINTER

def test_deepnote_runtime_bootstraps_engine_when_missing(tmp_path, monkeypatch):
    import chessgrandmaster.coordinator.deepnote_manual_worker as worker

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/setup_platform.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    venv = tmp_path / "venv"
    python = venv / "bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    engine = tmp_path / "stockfish"
    state = {"installed": False}
    calls = []

    def resolver():
        if not state["installed"]:
            raise FileNotFoundError("missing engine")
        return engine

    def fake_run(command, *, cwd, env, check):
        calls.append((command, cwd, dict(env), check))
        state["installed"] = True
        engine.write_text("stockfish", encoding="utf-8")

    monkeypatch.setattr(worker, "resolve_installed_engine", resolver)
    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setattr(worker.sys, "prefix", str(venv))
    monkeypatch.setattr(worker.sys, "executable", str(python))

    resolved = worker.ensure_deepnote_runtime_ready(repo_root=repo)

    assert resolved == engine
    assert len(calls) == 1
    command, cwd, env, check = calls[0]
    assert command == ["bash", str(repo / "scripts/setup_platform.sh")]
    assert cwd == repo
    assert env["CGM_VENV"] == str(venv)
    assert env["CGM_BOOTSTRAP_PYTHON"] == str(python)
    assert env["CGM_INSTALL_DEV"] == "0"
    assert check is True


def test_molab_s3_bridge_publishes_coordinator_batch(tmp_path: Path):
    from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge, CURRENT_POINTER as MOLAB_CURRENT

    coordinator = seed_coordinator(tmp_path, 2)
    storage = MemoryStorage()
    bridge = MolabS3Bridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "molab-bridge",
        max_jobs=2, min_priority=500,
    )

    current = bridge.ensure_current_batch()

    assert current is not None
    assert current.jobs == 2
    pointer = json.loads(storage.files[MOLAB_CURRENT])
    assert pointer["schema_version"] == "cgm-molab-s3-current-1"
    assert pointer["batch_id"] == current.batch_id
    assert pointer["archive_path"] == f"inbox/batches/{current.batch_id}.zip"
    assert hashlib.sha256(storage.files[pointer["archive_path"]]).hexdigest() == pointer["sha256"]


def test_molab_s3_worker_uploads_durable_shards_progress_and_ready(tmp_path: Path):
    from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge, READY_POINTER as MOLAB_READY
    from chessgrandmaster.coordinator.molab_s3_worker import run_molab_s3_cycle

    coordinator = seed_coordinator(tmp_path, 2)
    storage = MemoryStorage()
    bridge = MolabS3Bridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "molab-bridge",
        max_jobs=2, min_priority=500,
    )
    current = bridge.ensure_current_batch()
    assert current is not None

    summary = run_molab_s3_cycle(storage=storage, work_root=tmp_path / "molab-work", executor=fake_executor)

    progress = json.loads(storage.files[f"runtime/{current.batch_id}/progress.json"])
    ready = json.loads(storage.files[MOLAB_READY])
    assert progress["state"] == "complete"
    assert progress["jobs_completed"] == 2
    assert summary.executed == 2
    assert ready["schema_version"] == "cgm-molab-s3-ready-1"
    assert ready["transport"] == "project-tree"
    assert ready["result_root"] == f"runtime/{current.batch_id}/result"
    assert f"runtime/{current.batch_id}/result/results/0000/job-result.json" in storage.files
    assert f"runtime/{current.batch_id}/result/results/0001/job-result.json" in storage.files


def test_molab_s3_bridge_imports_result_tree_and_publishes_next(tmp_path: Path):
    from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge
    from chessgrandmaster.coordinator.molab_s3_worker import run_molab_s3_cycle

    coordinator = seed_coordinator(tmp_path, 3)
    storage = MemoryStorage()
    bridge = MolabS3Bridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "molab-bridge",
        max_jobs=2, min_priority=500,
    )
    first = bridge.ensure_current_batch()
    assert first is not None
    run_molab_s3_cycle(storage=storage, work_root=tmp_path / "molab-work", executor=fake_executor)

    processed = bridge.poll_once()

    assert processed is not None
    assert processed.batch_id == first.batch_id
    assert processed.completed == 2
    assert processed.rejected == 0
    assert processed.next_batch_id is not None
    assert len(coordinator.list_jobs("COMPLETED")) == 2


def test_molab_s3_publish_retry_does_not_lease_a_second_batch(tmp_path: Path):
    from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge, CURRENT_POINTER as MOLAB_CURRENT

    class FailPointerOnce(MemoryStorage):
        def __init__(self):
            super().__init__(); self.failed = False
        def upload(self, path: str, source: Path) -> None:
            if path == MOLAB_CURRENT and not self.failed:
                self.failed = True
                raise RuntimeError("temporary S3 pointer failure")
            super().upload(path, source)

    coordinator = seed_coordinator(tmp_path, 4)
    storage = FailPointerOnce()
    bridge = MolabS3Bridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "molab-bridge",
        max_jobs=2, min_priority=500,
    )
    try:
        bridge.ensure_current_batch()
    except RuntimeError as exc:
        assert "pointer failure" in str(exc)
    else:
        raise AssertionError("expected first publish to fail")
    exported_after_failure = len(coordinator.list_jobs("EXPORTED"))

    current = bridge.ensure_current_batch()

    assert current is not None
    assert exported_after_failure == 2
    assert len(coordinator.list_jobs("EXPORTED")) == 2


def test_molab_s3_bridge_recovers_complete_tree_without_ready_pointer(tmp_path: Path):
    from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge, READY_POINTER as MOLAB_READY
    from chessgrandmaster.coordinator.molab_s3_worker import run_molab_s3_cycle

    coordinator = seed_coordinator(tmp_path, 1)
    storage = MemoryStorage()
    bridge = MolabS3Bridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "molab-bridge",
        max_jobs=1, min_priority=500,
    )
    first = bridge.ensure_current_batch()
    assert first is not None
    run_molab_s3_cycle(storage=storage, work_root=tmp_path / "molab-work", executor=fake_executor)
    storage.delete(MOLAB_READY)

    processed = bridge.poll_once()

    assert processed is not None
    assert processed.batch_id == first.batch_id
    assert processed.completed == 1
    assert processed.rejected == 0


def test_molab_s3_worker_resumes_completed_remote_shards(tmp_path: Path):
    from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge, READY_POINTER as MOLAB_READY
    from chessgrandmaster.coordinator.molab_s3_worker import run_molab_s3_cycle

    coordinator = seed_coordinator(tmp_path, 2)
    storage = MemoryStorage()
    bridge = MolabS3Bridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "molab-bridge",
        max_jobs=2, min_priority=500,
    )
    bridge.ensure_current_batch()
    calls = []

    def fail_second(bundle: Path, result: Path, provider: str) -> Path:
        job_id = json.loads((bundle / "job.json").read_text())["job_id"]
        calls.append(job_id)
        if job_id == "job-1":
            raise RuntimeError("fixture interruption")
        return fake_executor(bundle, result, provider)

    try:
        run_molab_s3_cycle(storage=storage, work_root=tmp_path / "molab-work-1", executor=fail_second)
    except RuntimeError as exc:
        assert "fixture interruption" in str(exc)
    else:
        raise AssertionError("expected interrupted first cycle")
    assert "runtime/" in next(key for key in storage.files if key.endswith("results/0000/checksums.json"))
    assert MOLAB_READY not in storage.files

    resumed = run_molab_s3_cycle(storage=storage, work_root=tmp_path / "molab-work-2", executor=fake_executor)

    assert resumed.skipped == 1
    assert resumed.executed == 1
    assert MOLAB_READY in storage.files


def test_molab_s3_worker_uploads_shard_checksum_last(tmp_path: Path):
    from chessgrandmaster.coordinator.molab_s3_bridge import MolabS3Bridge
    from chessgrandmaster.coordinator.molab_s3_worker import run_molab_s3_cycle

    coordinator = seed_coordinator(tmp_path, 1)
    storage = MemoryStorage()
    bridge = MolabS3Bridge(
        coordinator=coordinator, storage=storage, root=tmp_path / "molab-bridge",
        max_jobs=1, min_priority=500,
    )
    current = bridge.ensure_current_batch()
    assert current is not None
    storage.upload_log.clear()

    run_molab_s3_cycle(storage=storage, work_root=tmp_path / "molab-work", executor=fake_executor)

    prefix = f"runtime/{current.batch_id}/result/results/0000/"
    shard_uploads = [path for path in storage.upload_log if path.startswith(prefix)]
    assert shard_uploads[-1] == prefix + "checksums.json"
