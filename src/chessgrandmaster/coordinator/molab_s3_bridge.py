"""Oracle-side coordinator bridge for Molab through S3-compatible storage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Protocol
import uuid

from .manual_batch import archive_directory, create_manual_batch, import_manual_result_directory
from .queue import Coordinator

CURRENT_POINTER = "inbox/current.json"
BATCH_INBOX = "inbox/batches"
READY_POINTER = "outbox/ready.json"


class MailboxStorage(Protocol):
    def read_bytes(self, path: str) -> bytes | None: ...
    def download(self, path: str, destination: Path) -> None: ...
    def upload(self, path: str, source: Path) -> None: ...
    def delete(self, path: str) -> None: ...


@dataclass(frozen=True)
class CurrentBatchSummary:
    batch_id: str
    jobs: int
    games: int
    plies: int
    sha256: str


@dataclass(frozen=True)
class ProcessedReadySummary:
    batch_id: str
    completed: int
    rejected: int
    next_batch_id: str | None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _safe_name(name: object) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError(f"invalid Molab result path: {name!r}")
    value = PurePosixPath(name)
    if value.is_absolute() or any(part in ("", ".", "..") for part in value.parts):
        raise ValueError(f"unsafe Molab result path: {name!r}")
    return value.as_posix()


def batch_archive_path(batch_id: str) -> str:
    value = str(batch_id)
    if not value or "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError(f"invalid Molab batch_id: {batch_id!r}")
    return f"{BATCH_INBOX}/{value}.zip"


class MolabS3Bridge:
    def __init__(self, *, coordinator: Coordinator, storage: MailboxStorage, root: str | Path,
                 max_jobs: int = 8, min_priority: int | None = 500, max_plies: int | None = None) -> None:
        self.coordinator = coordinator
        self.storage = storage
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_jobs = int(max_jobs)
        self.min_priority = min_priority
        self.max_plies = max_plies
        self.state_path = self.root / "state.json"

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.is_file():
            return {"schema_version": "cgm-molab-s3-bridge-state-1"}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save_state(self, state: dict[str, object]) -> None:
        state["schema_version"] = "cgm-molab-s3-bridge-state-1"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(self.state_path, state)

    @staticmethod
    def _batch_totals(batch_path: Path) -> tuple[int, int]:
        games = plies = 0
        for job_path in sorted((batch_path / "jobs").glob("*/job.json")):
            source = json.loads(job_path.read_text(encoding="utf-8")).get("input") or {}
            games += int(source.get("games") or 0)
            plies += int(source.get("plies") or 0)
        return games, plies

    def _upload_pending(self, state: dict[str, object]) -> CurrentBatchSummary:
        pending = dict(state["pending_publish"])
        batch_id = str(pending["batch_id"])
        archive = Path(str(pending["archive"]))
        pointer_file = Path(str(pending["pointer_file"]))
        remote_archive = str(pending["remote_archive"])
        if not pending.get("archive_uploaded"):
            self.storage.upload(remote_archive, archive)
            pending["archive_uploaded"] = True
            state["pending_publish"] = pending
            self._save_state(state)
        # The small mutable pointer is always published last.
        self.storage.upload(CURRENT_POINTER, pointer_file)
        current = {
            "batch_id": batch_id, "jobs": int(pending["jobs"]),
            "games": int(pending["games"]), "plies": int(pending["plies"]),
            "sha256": str(pending["sha256"]), "archive": str(archive),
            "remote_archive": remote_archive,
        }
        state["current"] = current
        state.pop("pending_publish", None)
        self._save_state(state)
        return CurrentBatchSummary(batch_id, current["jobs"], current["games"], current["plies"], current["sha256"])

    def ensure_current_batch(self) -> CurrentBatchSummary | None:
        state = self._load_state()
        if state.get("pending_publish"):
            return self._upload_pending(state)
        current = state.get("current")
        if isinstance(current, dict) and current.get("batch_id"):
            return CurrentBatchSummary(str(current["batch_id"]), int(current["jobs"]), int(current["games"]),
                                       int(current["plies"]), str(current["sha256"]))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.root / "batches" / f"molab-{stamp}-{uuid.uuid4().hex[:8]}"
        summary = create_manual_batch(
            coordinator=self.coordinator, destination=destination, provider="molab",
            max_jobs=self.max_jobs, min_priority=self.min_priority, max_plies=self.max_plies,
        )
        if summary.jobs == 0:
            shutil.rmtree(destination, ignore_errors=True)
            return None
        games, plies = self._batch_totals(summary.path)
        outgoing = self.root / "outgoing"
        archive = archive_directory(summary.path, outgoing / f"{summary.batch_id}.zip")
        sha = _sha256(archive)
        remote_archive = batch_archive_path(summary.batch_id)
        pointer = {
            "schema_version": "cgm-molab-s3-current-1", "batch_id": summary.batch_id,
            "archive_path": remote_archive, "sha256": sha, "jobs": summary.jobs,
            "games": games, "plies": plies, "created_at": datetime.now(timezone.utc).isoformat(),
        }
        pointer_file = outgoing / f"{summary.batch_id}.current.json"
        _atomic_json(pointer_file, pointer)
        state["pending_publish"] = {
            "batch_id": summary.batch_id, "jobs": summary.jobs, "games": games, "plies": plies,
            "sha256": sha, "archive": str(archive), "pointer_file": str(pointer_file),
            "remote_archive": remote_archive, "archive_uploaded": False,
        }
        self._save_state(state)
        return self._upload_pending(state)

    def _download_project_tree(self, *, batch_id: str, remote_root: str,
                               expected_checksums_sha: str) -> tuple[Path, tuple[str, ...]]:
        checksums_remote = f"{remote_root}/checksums.json"
        checksums_bytes = self.storage.read_bytes(checksums_remote)
        if checksums_bytes is None:
            raise ValueError("Molab result tree has no checksums.json")
        if _sha256_bytes(checksums_bytes) != expected_checksums_sha:
            raise ValueError("Molab result checksum manifest mismatch")
        payload = json.loads(checksums_bytes.decode("utf-8"))
        if payload.get("schema_version") != "cgm-checksums-1":
            raise ValueError("Molab result tree has invalid checksum schema")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("Molab result tree checksum file set is empty")
        names = tuple(_safe_name(name) for name in files)
        incoming = self.root / "incoming" / batch_id
        if incoming.exists():
            shutil.rmtree(incoming)
        incoming.mkdir(parents=True)
        (incoming / "checksums.json").write_bytes(checksums_bytes)
        for name in names:
            expected = str(files[name]).lower()
            local = incoming / Path(*PurePosixPath(name).parts)
            self.storage.download(f"{remote_root}/{name}", local)
            if _sha256(local) != expected:
                raise ValueError(f"Molab result file checksum mismatch for {name}")
        return incoming, names

    def _ready_from_completed_current(self, state: dict[str, object]) -> dict[str, object] | None:
        current = state.get("current")
        if not isinstance(current, dict):
            return None
        batch_id = str(current.get("batch_id") or "")
        if not batch_id:
            return None
        result_root = f"runtime/{batch_id}/result"
        checksums = self.storage.read_bytes(f"{result_root}/checksums.json")
        if checksums is None:
            return None
        return {
            "schema_version": "cgm-molab-s3-ready-1", "transport": "project-tree",
            "batch_id": batch_id, "result_root": result_root,
            "checksums_sha256": _sha256_bytes(checksums),
        }

    def poll_once(self) -> ProcessedReadySummary | None:
        state = self._load_state()
        ready_bytes = self.storage.read_bytes(READY_POINTER)
        if ready_bytes is None:
            ready = self._ready_from_completed_current(state)
            if ready is None:
                self.ensure_current_batch()
                return None
        else:
            ready = json.loads(ready_bytes.decode("utf-8"))
        if ready.get("schema_version") != "cgm-molab-s3-ready-1" or ready.get("transport") != "project-tree":
            raise ValueError("invalid Molab ready pointer")
        batch_id = str(ready.get("batch_id") or "")
        current = state.get("current")
        if not isinstance(current, dict) or str(current.get("batch_id") or "") != batch_id:
            raise ValueError(f"unexpected Molab result batch: {batch_id}")
        remote_root = str(ready.get("result_root") or "")
        expected_root = f"runtime/{batch_id}/result"
        if remote_root != expected_root:
            raise ValueError("invalid Molab result root")
        expected_sha = str(ready.get("checksums_sha256") or "").lower()
        incoming, remote_names = self._download_project_tree(
            batch_id=batch_id, remote_root=remote_root, expected_checksums_sha=expected_sha,
        )
        imported = import_manual_result_directory(coordinator=self.coordinator, result_root=incoming)
        if imported.rejected:
            return ProcessedReadySummary(batch_id, imported.completed, imported.rejected, None)

        state = self._load_state()
        state["last_processed_batch_id"] = batch_id
        state.pop("current", None)
        self._save_state(state)
        self.storage.delete(READY_POINTER)
        self.storage.delete(CURRENT_POINTER)
        self.storage.delete(batch_archive_path(batch_id))
        for name in remote_names:
            self.storage.delete(f"{remote_root}/{name}")
        self.storage.delete(f"{remote_root}/checksums.json")
        self.storage.delete(f"runtime/{batch_id}/progress.json")
        next_batch = self.ensure_current_batch()
        return ProcessedReadySummary(batch_id, imported.completed, 0,
                                     None if next_batch is None else next_batch.batch_id)
