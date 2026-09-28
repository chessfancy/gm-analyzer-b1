from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from chessgrandmaster.coordinator import Coordinator, write_checksums
from chessgrandmaster.coordinator.deepnote_manual_bridge import (
    CURRENT_ARCHIVE,
    CURRENT_POINTER,
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
    (inbox / "current-batch.zip").write_bytes(storage.files[CURRENT_ARCHIVE])
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
