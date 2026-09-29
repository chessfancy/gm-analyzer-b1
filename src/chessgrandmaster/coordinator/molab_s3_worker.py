"""Molab one-click manual worker cycle backed by S3-compatible mailbox storage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil

from .deepnote_manual_worker import _load_manual_runner
from .manual_batch import safe_extract_zip
from .molab_s3_bridge import CURRENT_POINTER, READY_POINTER, MailboxStorage, batch_archive_path


@dataclass(frozen=True)
class MolabCycleSummary:
    batch_id: str
    executed: int
    skipped: int
    results: int
    checksums_sha256: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_upload_json(storage: MailboxStorage, local: Path, remote: str, payload: dict[str, object]) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    storage.upload(remote, local)


def _restore_result_bundle(storage: MailboxStorage, remote_root: str, local_root: Path) -> bool:
    raw = storage.read_bytes(f"{remote_root}/checksums.json")
    if raw is None:
        return False
    payload = json.loads(raw.decode("utf-8"))
    files = payload.get("files")
    if payload.get("schema_version") != "cgm-checksums-1" or not isinstance(files, dict):
        return False
    local_root.mkdir(parents=True, exist_ok=True)
    for name in files:
        posix = PurePosixPath(str(name))
        if posix.is_absolute() or ".." in posix.parts:
            raise ValueError(f"unsafe Molab persisted result path: {name}")
        storage.download(f"{remote_root}/{posix.as_posix()}", local_root / Path(*posix.parts))
    (local_root / "checksums.json").write_bytes(raw)
    return True


def _upload_tree(storage: MailboxStorage, local_root: Path, remote_root: str) -> None:
    files = sorted(item for item in local_root.rglob("*") if item.is_file())
    checksum = local_root / "checksums.json"
    for path in files:
        if path == checksum:
            continue
        rel = path.relative_to(local_root).as_posix()
        storage.upload(f"{remote_root}/{rel}", path)
    if checksum.is_file():
        storage.upload(f"{remote_root}/checksums.json", checksum)


def run_molab_s3_cycle(*, storage: MailboxStorage, work_root: str | Path, executor=None) -> MolabCycleSummary:
    work_root = Path(work_root).expanduser().resolve()
    current_bytes = storage.read_bytes(CURRENT_POINTER)
    if current_bytes is None:
        raise FileNotFoundError("Molab S3 current batch pointer not found")
    current = json.loads(current_bytes.decode("utf-8"))
    if current.get("schema_version") != "cgm-molab-s3-current-1":
        raise ValueError("invalid Molab S3 current pointer schema")
    batch_id = str(current.get("batch_id") or "")
    archive_rel = str(current.get("archive_path") or "")
    if archive_rel != batch_archive_path(batch_id):
        raise ValueError("unexpected Molab S3 batch archive path")

    runtime = work_root / "runtime" / batch_id
    archive = runtime / "batch.zip"
    storage.download(archive_rel, archive)
    expected_sha = str(current.get("sha256") or "").lower()
    if _sha256(archive) != expected_sha:
        raise ValueError("Molab S3 batch SHA256 mismatch")
    batch_dir = runtime / "batch"
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    safe_extract_zip(archive, batch_dir)
    batch = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    if str(batch.get("batch_id") or "") != batch_id:
        raise ValueError("Molab S3 pointer batch_id does not match archive")

    result_dir = runtime / "result"
    remote_result_root = f"runtime/{batch_id}/result"
    totals = {"jobs": 0, "games": 0, "plies": 0}
    for item in batch.get("jobs", []):
        ordinal = int(item["ordinal"])
        bundle = batch_dir / str(item["path"])
        source = json.loads((bundle / "job.json").read_text(encoding="utf-8")).get("input") or {}
        totals["jobs"] += 1; totals["games"] += int(source.get("games") or 0); totals["plies"] += int(source.get("plies") or 0)
        _restore_result_bundle(storage, f"{remote_result_root}/results/{ordinal:04d}", result_dir / "results" / f"{ordinal:04d}")

    progress = {
        "schema_version": "cgm-molab-s3-progress-1", "batch_id": batch_id, "state": "running",
        "jobs_total": totals["jobs"], "jobs_completed": 0, "games_total": totals["games"], "games_completed": 0,
        "plies_total": totals["plies"], "plies_completed": 0, "executed": 0, "skipped": 0,
        "last_completed_job": None, "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    progress_local = runtime / "progress.json"
    _write_upload_json(storage, progress_local, f"runtime/{batch_id}/progress.json", progress)

    def report(event: dict[str, object]) -> None:
        ordinal = int(event["ordinal"])
        local_bundle = result_dir / "results" / f"{ordinal:04d}"
        _upload_tree(storage, local_bundle, f"{remote_result_root}/results/{ordinal:04d}")
        progress["jobs_completed"] = int(event["completed_jobs"])
        progress["games_completed"] = int(progress["games_completed"]) + int(event["games"])
        progress["plies_completed"] = int(progress["plies_completed"]) + int(event["plies"])
        progress["executed"] = int(event["executed"]); progress["skipped"] = int(event["skipped"])
        progress["last_completed_job"] = str(event["job_id"])
        progress["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_upload_json(storage, progress_local, f"runtime/{batch_id}/progress.json", progress)

    try:
        summary = _load_manual_runner()(batch_dir, result_dir, executor=executor, progress_callback=report)
    except Exception:
        progress["state"] = "interrupted"; progress["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_upload_json(storage, progress_local, f"runtime/{batch_id}/progress.json", progress)
        raise

    progress["state"] = "complete"; progress["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_upload_json(storage, progress_local, f"runtime/{batch_id}/progress.json", progress)
    storage.upload(f"{remote_result_root}/batch-result.json", result_dir / "batch-result.json")
    checksums = result_dir / "checksums.json"
    checksums_sha = _sha256(checksums)
    storage.upload(f"{remote_result_root}/checksums.json", checksums)
    ready = {
        "schema_version": "cgm-molab-s3-ready-1", "transport": "project-tree", "batch_id": batch_id,
        "result_root": remote_result_root, "checksums_sha256": checksums_sha, "results": summary.results,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_upload_json(storage, runtime / "ready.json", READY_POINTER, ready)
    return MolabCycleSummary(batch_id, summary.executed, summary.skipped, summary.results, checksums_sha)
