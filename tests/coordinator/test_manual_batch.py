from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from chessgrandmaster.coordinator import Coordinator, JobState, validate_job_bundle, write_checksums
from chessgrandmaster.coordinator.manual_batch import (
    archive_directory,
    create_manual_batch,
    import_manual_result_archive,
    safe_extract_zip,
)


def make_job_bundle(root: Path, job_id: str, fingerprint: str) -> Path:
    root.mkdir(parents=True)
    input_bytes = f'[Event "{job_id}"]\n\n1. e4 e5 *\n'.encode()
    (root / "input.pgn").write_bytes(input_bytes)
    job = {
        "schema_version": "cgm-job-1",
        "job_id": job_id,
        "tournament_id": "manual-tournament",
        "tournament_revision": 1,
        "shard_index": int(job_id.rsplit("-", 1)[-1]),
        "input": {
            "key": f"jobs/{job_id}/input.pgn",
            "sha256": hashlib.sha256(input_bytes).hexdigest(),
            "games": 1,
            "plies": 2,
            "canonical_game_fingerprints": [fingerprint],
        },
        "analysis": {"pipeline_version": 3, "engine": "stockfish19", "depth": 19},
        "config_hash": "cfg-manual",
    }
    manifest = {
        "schema_version": "cgm-tournament-1",
        "tournament_id": "manual-tournament",
        "revision": 1,
        "shards": [{"job_id": job_id, "shard_index": job["shard_index"]}],
    }
    (root / "job.json").write_text(json.dumps(job, sort_keys=True), encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    write_checksums(root)
    return root


def make_result_bundle(root: Path, job_bundle: Path) -> Path:
    root.mkdir(parents=True)
    expected = validate_job_bundle(job_bundle)
    job = expected.job_json
    result = {
        "schema_version": "cgm-job-result-1",
        "job_id": expected.job_id,
        "config_hash": expected.config_hash,
        "tournament_id": job["tournament_id"],
        "tournament_revision": job["tournament_revision"],
        "shard_index": job["shard_index"],
        "input": {
            "key": job["input"]["key"],
            "sha256": job["input"]["sha256"],
            "canonical_game_fingerprints": job["input"]["canonical_game_fingerprints"],
        },
    }
    (root / "job-result.json").write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    (root / "analysis.sqlite").write_bytes(b"sqlite-result")
    (root / "Mistakes.pgn").write_text("", encoding="utf-8")
    (root / "Blunders.pgn").write_text("", encoding="utf-8")
    (root / "raw-uci").mkdir()
    (root / "raw-uci" / "trace.jsonl.gz").write_bytes(b"uci")
    write_checksums(root)
    return root


def seed_coordinator(tmp_path: Path, count: int = 2) -> Coordinator:
    coordinator = Coordinator(tmp_path / "coordinator.sqlite", archive_root=tmp_path / "archive")
    for idx in range(count):
        bundle = make_job_bundle(tmp_path / f"job-{idx}", f"job-{idx}", f"fp-{idx}")
        coordinator.register_job(bundle, priority=500)
    return coordinator


def test_create_manual_batch_exports_individual_coordinator_jobs(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 2)

    summary = create_manual_batch(
        coordinator=coordinator,
        destination=tmp_path / "batch",
        provider="molab",
        max_jobs=2,
        min_priority=500,
        lease_seconds=86400,
    )

    payload = json.loads((tmp_path / "batch" / "batch.json").read_text(encoding="utf-8"))
    assert summary.jobs == 2
    assert payload["runtime_provider"] == "molab-marimo"
    assert payload["attempt_provider"] == "molab-manual-batch"
    assert [item["job_id"] for item in payload["jobs"]] == ["job-0", "job-1"]
    for index, item in enumerate(payload["jobs"]):
        bundle = tmp_path / "batch" / item["path"]
        assert validate_job_bundle(bundle).job_id == f"job-{index}"
        assert coordinator.get_job(f"job-{index}").state is JobState.EXPORTED
    assert (tmp_path / "batch" / "checksums.json").is_file()


def test_manual_result_archive_import_completes_all_jobs(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 2)
    create_manual_batch(
        coordinator=coordinator,
        destination=tmp_path / "batch",
        provider="deepnote",
        max_jobs=2,
        min_priority=500,
    )
    batch = json.loads((tmp_path / "batch" / "batch.json").read_text(encoding="utf-8"))
    results = tmp_path / "manual-results"
    for item in batch["jobs"]:
        make_result_bundle(results / "results" / f"{item['ordinal']:04d}", tmp_path / "batch" / item["path"])
    (results / "batch-result.json").write_text(
        json.dumps({"schema_version": "cgm-manual-result-batch-1", "batch_id": batch["batch_id"]}),
        encoding="utf-8",
    )
    write_checksums(results)
    archive = archive_directory(results, tmp_path / "manual-results.zip")

    summary = import_manual_result_archive(
        coordinator=coordinator,
        archive_path=archive,
        extract_root=tmp_path / "imports",
    )

    assert summary.completed == 2
    assert summary.rejected == 0
    assert coordinator.get_job("job-0").state is JobState.COMPLETED
    assert coordinator.get_job("job-1").state is JobState.COMPLETED


def test_safe_extract_zip_rejects_path_traversal(tmp_path: Path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escape.txt", "nope")

    with pytest.raises(ValueError, match="unsafe"):
        safe_extract_zip(archive, tmp_path / "extract")
    assert not (tmp_path / "escape.txt").exists()


def load_manual_runner():
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "scripts" / "workers" / "run_manual_batch.py"
    spec = importlib.util.spec_from_file_location("cgm_run_manual_batch", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_manual_worker_executes_each_job_and_resumes_verified_results(tmp_path: Path):
    coordinator = seed_coordinator(tmp_path, 2)
    create_manual_batch(
        coordinator=coordinator,
        destination=tmp_path / "batch",
        provider="molab",
        max_jobs=2,
        min_priority=500,
    )
    runner = load_manual_runner()
    calls: list[tuple[str, str]] = []

    def fake_executor(bundle: Path, result: Path, provider: str) -> Path:
        calls.append((validate_job_bundle(bundle).job_id, provider))
        return make_result_bundle(result, bundle)

    first = runner.run_manual_batch(tmp_path / "batch", tmp_path / "results", executor=fake_executor)
    second = runner.run_manual_batch(tmp_path / "batch", tmp_path / "results", executor=fake_executor)

    assert first.executed == 2
    assert first.skipped == 0
    assert second.executed == 0
    assert second.skipped == 2
    assert calls == [("job-0", "molab-marimo"), ("job-1", "molab-marimo")]
    payload = json.loads((tmp_path / "results" / "batch-result.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "cgm-manual-result-batch-1"
    assert [item["job_id"] for item in payload["results"]] == ["job-0", "job-1"]
    assert (tmp_path / "results" / "checksums.json").is_file()


def load_job_executor_module():
    import importlib.util
    import sys

    path = Path(__file__).resolve().parents[2] / "scripts" / "workers" / "run_job_bundle.py"
    spec = importlib.util.spec_from_file_location("cgm_run_job_bundle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_job_executor_preserves_partial_work_root_for_pipeline_resume(tmp_path: Path, monkeypatch):
    bundle = make_job_bundle(tmp_path / "bundle", "job-0", "fp-0")
    result = tmp_path / "results" / "0000"
    work_root = result.parent / ".0000-work"
    work_root.mkdir(parents=True)
    marker = work_root / "resume-marker.txt"
    marker.write_text("keep", encoding="utf-8")
    module = load_job_executor_module()

    def fake_run_pipeline(input_pgn, *, root, **kwargs):
        root = Path(root)
        assert (root / "resume-marker.txt").read_text(encoding="utf-8") == "keep"
        db = root / "db" / "analysis.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"sqlite")
        out = root / "output"
        out.mkdir(parents=True, exist_ok=True)
        (out / "Mistakes_test.pgn").write_text("", encoding="utf-8")
        (out / "Blunders_test.pgn").write_text("", encoding="utf-8")
        uci = root / "uci" / "run_1"
        uci.mkdir(parents=True, exist_ok=True)
        (uci / "trace.jsonl.gz").write_bytes(b"uci")
        return {"database": str(db), "run_id": 1, "games": 1, "moves": 2,
                "mistakes": {}, "blunders": {}}

    monkeypatch.setattr(module, "run_pipeline", fake_run_pipeline)
    out = module.execute_job(bundle, result, "deepnote")
    assert out == result.resolve()
    assert (out / "analysis.sqlite").is_file()


def test_worker_loop_sends_results_over_synchronous_connection(monkeypatch):
    import chessgrandmaster.parallel_runner as module
    from types import SimpleNamespace

    class FakeEngine:
        def __init__(self, *args, **kwargs):
            pass
        def analyze_move(self, *args, **kwargs):
            return SimpleNamespace(
                played_rank=0, lucas_loss=0, category="NO_RATING", nag=0,
                second_search=False, responses=[], depth_snapshots=[],
            )
        def close(self):
            pass

    class JobQueue:
        def __init__(self):
            self.items = iter([{
                "job_id": "j", "game_id": 1, "source_game_index": 1,
                "move_id": 1, "ply": 1, "fen_before": "x", "played_uci": "y",
            }, None])
        def get(self):
            return next(self.items)

    class ResultConnection:
        def __init__(self):
            self.messages = []
            self.closed = False
        def send(self, message):
            self.messages.append(message)
        def close(self):
            self.closed = True

    monkeypatch.setattr(module, "LucasEngineWorker", FakeEngine)
    results = ResultConnection()
    module._worker_loop(
        0, "/bin/false", {
            "threads": 1, "hash_mb": 16, "multipv": 1, "depth": 19,
            "time_sec": 0.0, "nodes": 0, "snapshot_depths": (19,),
        }, JobQueue(), results,
    )
    assert [item["type"] for item in results.messages] == ["job_result", "worker_finished"]
    assert results.closed is True


def test_parent_receives_all_messages_from_closed_synchronous_pipe(monkeypatch):
    from chessgrandmaster.parallel_runner import ParallelLucasRunner
    import multiprocessing as mp

    class CleanProcess:
        exitcode = 0
        def is_alive(self):
            return False

    class SinkQueue:
        def put_nowait(self, value):
            return None

    receiver, sender = mp.Pipe(duplex=False)
    sender.send({
        "type": "job_result",
        "ok": True,
        "job_id": "j",
    })
    sender.send({
        "type": "worker_finished",
        "worker_id": 0,
        "engine_session_id": "session",
        "archive_manifest": None,
    })
    sender.close()

    runner = ParallelLucasRunner("/bin/false", workers=1, depth=19)

    def fake_start():
        runner.processes = [CleanProcess()]
        runner.job_queues = [SinkQueue()]
        runner.result_receivers = [receiver]

    monkeypatch.setattr(runner, "start", fake_start)
    jobs = [{"job_id": "j", "game_id": 1, "ply": 1}]
    results = list(runner.iter_analyze(jobs))

    assert [item["job_id"] for item in results] == ["j"]

def test_dead_worker_error_reports_exit_codes(monkeypatch):
    from chessgrandmaster.parallel_runner import ParallelLucasRunner
    import queue

    class DeadProcess:
        exitcode = -9
        def is_alive(self):
            return False

    class SinkQueue:
        def put_nowait(self, value):
            return None

    runner = ParallelLucasRunner("/bin/false", workers=1, depth=19)
    runner.result_queue = queue.Queue()

    def fake_start():
        runner.processes = [DeadProcess()]
        runner.job_queues = [SinkQueue()]

    monkeypatch.setattr(runner, "start", fake_start)
    jobs = [{"job_id": "j", "game_id": 1, "ply": 1, "fen_before": "x", "played_uci": "y"}]
    with pytest.raises(RuntimeError, match=r"exitcode=-9"):
        list(runner.iter_analyze(jobs))
