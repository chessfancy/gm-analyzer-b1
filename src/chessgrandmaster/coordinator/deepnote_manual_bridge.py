"""Oracle-side bridge between coordinator manual batches and Deepnote project storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from typing import Protocol
import uuid
import zipfile

from .manual_batch import (
    archive_directory,
    create_manual_batch,
    import_manual_result_archive,
    import_manual_result_directory,
)
from .queue import Coordinator

CURRENT_ARCHIVE = "cgm-manual/inbox/current-batch.zip"
CURRENT_SHA = "cgm-manual/inbox/current-batch.zip.sha256"
CURRENT_POINTER = "cgm-manual/inbox/current.json"
READY_POINTER = "cgm-manual/outbox/ready.json"


class ProjectStorage(Protocol):
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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temp.replace(path)


def _safe_tree_name(name: object) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError(f"invalid Deepnote result path: {name!r}")
    value = PurePosixPath(name)
    if value.is_absolute() or any(part in ("", ".", "..") for part in value.parts):
        raise ValueError(f"unsafe Deepnote result path: {name!r}")
    return value.as_posix()


class DeepnoteManualBridge:
    def __init__(
        self,
        *,
        coordinator: Coordinator,
        storage: ProjectStorage,
        root: str | Path,
        max_jobs: int = 8,
        min_priority: int | None = 500,
        max_plies: int | None = None,
    ) -> None:
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
            return {"schema_version": "cgm-deepnote-bridge-state-1"}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save_state(self, state: dict[str, object]) -> None:
        state["schema_version"] = "cgm-deepnote-bridge-state-1"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(self.state_path, state)

    @staticmethod
    def _batch_totals(batch_path: Path) -> tuple[int, int]:
        games = 0
        plies = 0
        for job_path in sorted((batch_path / "jobs").glob("*/job.json")):
            payload = json.loads(job_path.read_text(encoding="utf-8"))
            source = payload.get("input") or {}
            games += int(source.get("games") or 0)
            plies += int(source.get("plies") or 0)
        return games, plies

    def _upload_pending(self, state: dict[str, object]) -> CurrentBatchSummary:
        pending = dict(state["pending_publish"])
        archive = Path(str(pending["archive"]))
        pointer_file = Path(str(pending["pointer_file"]))
        sha_file = Path(str(pending["sha_file"]))
        self.storage.delete(CURRENT_POINTER)
        self.storage.upload(CURRENT_ARCHIVE, archive)
        self.storage.upload(CURRENT_SHA, sha_file)
        self.storage.upload(CURRENT_POINTER, pointer_file)
        current = {
            "batch_id": pending["batch_id"],
            "jobs": pending["jobs"],
            "games": pending["games"],
            "plies": pending["plies"],
            "sha256": pending["sha256"],
            "archive": str(archive),
            "pointer_file": str(pointer_file),
            "sha_file": str(sha_file),
        }
        state["current"] = current
        state.pop("pending_publish", None)
        self._save_state(state)
        return CurrentBatchSummary(
            batch_id=str(current["batch_id"]),
            jobs=int(current["jobs"]),
            games=int(current["games"]),
            plies=int(current["plies"]),
            sha256=str(current["sha256"]),
        )

    def ensure_current_batch(self) -> CurrentBatchSummary | None:
        state = self._load_state()
        if state.get("pending_publish"):
            return self._upload_pending(state)
        current = state.get("current")
        if isinstance(current, dict) and current.get("batch_id"):
            return CurrentBatchSummary(
                batch_id=str(current["batch_id"]),
                jobs=int(current["jobs"]),
                games=int(current["games"]),
                plies=int(current["plies"]),
                sha256=str(current["sha256"]),
            )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = self.root / "batches" / f"deepnote-{stamp}-{uuid.uuid4().hex[:8]}"
        summary = create_manual_batch(
            coordinator=self.coordinator,
            destination=destination,
            provider="deepnote",
            max_jobs=self.max_jobs,
            min_priority=self.min_priority,
            max_plies=self.max_plies,
        )
        if summary.jobs == 0:
            shutil.rmtree(destination, ignore_errors=True)
            return None
        games, plies = self._batch_totals(summary.path)
        outgoing = self.root / "outgoing"
        outgoing.mkdir(parents=True, exist_ok=True)
        archive = archive_directory(summary.path, outgoing / f"{summary.batch_id}.zip")
        sha = _sha256(archive)
        sha_file = archive.with_suffix(".zip.sha256")
        sha_file.write_text(f"{sha}  {archive.name}\n", encoding="utf-8")
        pointer = {
            "schema_version": "cgm-deepnote-current-1",
            "batch_id": summary.batch_id,
            "archive_path": CURRENT_ARCHIVE,
            "sha256": sha,
            "jobs": summary.jobs,
            "games": games,
            "plies": plies,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        pointer_file = outgoing / f"{summary.batch_id}.current.json"
        _atomic_json(pointer_file, pointer)
        state["pending_publish"] = {
            "batch_id": summary.batch_id,
            "jobs": summary.jobs,
            "games": games,
            "plies": plies,
            "sha256": sha,
            "archive": str(archive),
            "sha_file": str(sha_file),
            "pointer_file": str(pointer_file),
        }
        self._save_state(state)
        return self._upload_pending(state)

    @staticmethod
    def _validate_ready(payload: dict[str, object]) -> tuple[str, str, str, str]:
        schema = payload.get("schema_version")
        batch_id = str(payload.get("batch_id") or "")
        if not batch_id:
            raise ValueError("Deepnote ready pointer has no batch_id")

        if schema == "cgm-deepnote-ready-1":
            archive_path = str(payload.get("archive_path") or "")
            sha = str(payload.get("sha256") or "").lower()
            if not archive_path.startswith("cgm-manual/outbox/") or "/../" in archive_path:
                raise ValueError("invalid Deepnote ready pointer identity")
            if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
                raise ValueError("invalid Deepnote result SHA256")
            return "archive", batch_id, archive_path, sha

        if schema == "cgm-deepnote-ready-2":
            if payload.get("transport") != "project-tree":
                raise ValueError("invalid Deepnote result transport")
            result_root = str(payload.get("result_root") or "")
            expected_root = f"cgm-manual/runtime/{batch_id}/result"
            if result_root != expected_root:
                raise ValueError("invalid Deepnote project-tree result root")
            checksums_path = str(payload.get("checksums_path") or "")
            if checksums_path != f"{expected_root}/checksums.json":
                raise ValueError("invalid Deepnote project-tree checksum path")
            sha = str(payload.get("checksums_sha256") or "").lower()
            if len(sha) != 64 or any(char not in "0123456789abcdef" for char in sha):
                raise ValueError("invalid Deepnote project-tree checksum SHA256")
            return "project-tree", batch_id, result_root, sha

        raise ValueError("invalid Deepnote ready pointer schema")

    def _ready_from_completed_current(
        self,
        state: dict[str, object],
    ) -> dict[str, object] | None:
        """Recover a completed project tree when Deepnote stopped before ready.json."""
        current = state.get("current")
        if not isinstance(current, dict):
            return None
        batch_id = str(current.get("batch_id") or "")
        if not batch_id:
            return None
        result_root = f"cgm-manual/runtime/{batch_id}/result"
        checksums_path = f"{result_root}/checksums.json"
        checksums_bytes = self.storage.read_bytes(checksums_path)
        if checksums_bytes is None:
            return None
        return {
            "schema_version": "cgm-deepnote-ready-2",
            "transport": "project-tree",
            "batch_id": batch_id,
            "result_root": result_root,
            "checksums_path": checksums_path,
            "checksums_sha256": _sha256_bytes(checksums_bytes),
        }

    def _download_project_tree(
        self,
        *,
        batch_id: str,
        remote_root: str,
        expected_checksums_sha: str,
    ) -> tuple[Path, tuple[str, ...]]:
        checksums_remote = f"{remote_root}/checksums.json"
        checksums_bytes = self.storage.read_bytes(checksums_remote)
        if checksums_bytes is None:
            raise ValueError("Deepnote project tree has no checksums.json")
        actual_manifest_sha = _sha256_bytes(checksums_bytes)
        if actual_manifest_sha != expected_checksums_sha:
            raise ValueError(
                "Deepnote project-tree checksum manifest mismatch: "
                f"expected {expected_checksums_sha}, got {actual_manifest_sha}"
            )
        payload = json.loads(checksums_bytes.decode("utf-8"))
        if payload.get("schema_version") != "cgm-checksums-1":
            raise ValueError("Deepnote project tree has invalid checksum schema")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise ValueError("Deepnote project tree checksum file set is empty")
        names = tuple(_safe_tree_name(name) for name in files)
        if list(names) != sorted(names):
            raise ValueError("Deepnote project tree checksum paths are not sorted")

        incoming = self.root / "incoming" / batch_id
        if incoming.exists():
            shutil.rmtree(incoming)
        incoming.mkdir(parents=True, exist_ok=True)
        (incoming / "checksums.json").write_bytes(checksums_bytes)
        for name in names:
            digest = files[name]
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError(f"invalid Deepnote checksum for {name}")
            local = incoming / Path(*PurePosixPath(name).parts)
            self.storage.download(f"{remote_root}/{name}", local)
            actual = _sha256(local)
            if actual != digest.lower():
                raise ValueError(
                    f"Deepnote project-tree file checksum mismatch for {name}"
                )
        return incoming, names

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
        transport, batch_id, result_location, expected_sha = self._validate_ready(ready)
        last = str(state.get("last_processed_batch_id") or "")
        if batch_id == last:
            self.storage.delete(READY_POINTER)
            if transport == "archive":
                self.storage.delete(result_location)
            self.ensure_current_batch()
            return None
        current = state.get("current")
        if not isinstance(current, dict) or str(current.get("batch_id") or "") != batch_id:
            raise ValueError(f"unexpected Deepnote result batch: {batch_id}")

        remote_names: tuple[str, ...] = ()
        if transport == "archive":
            incoming = self.root / "incoming" / f"{batch_id}.zip"
            self.storage.download(result_location, incoming)
            actual_sha = _sha256(incoming)
            if actual_sha != expected_sha:
                raise ValueError(
                    f"Deepnote result SHA256 mismatch: expected {expected_sha}, got {actual_sha}"
                )
            with zipfile.ZipFile(incoming) as handle:
                try:
                    result_meta = json.loads(handle.read("batch-result.json"))
                except KeyError as exc:
                    raise ValueError("Deepnote result archive has no batch-result.json") from exc
            if str(result_meta.get("batch_id") or "") != batch_id:
                raise ValueError("Deepnote ready pointer batch_id does not match result archive")
            imported = import_manual_result_archive(
                coordinator=self.coordinator,
                archive_path=incoming,
                extract_root=self.root / "imports",
            )
        else:
            incoming, remote_names = self._download_project_tree(
                batch_id=batch_id,
                remote_root=result_location,
                expected_checksums_sha=expected_sha,
            )
            result_meta = json.loads((incoming / "batch-result.json").read_text(encoding="utf-8"))
            if str(result_meta.get("batch_id") or "") != batch_id:
                raise ValueError("Deepnote ready pointer batch_id does not match result tree")
            imported = import_manual_result_directory(
                coordinator=self.coordinator,
                result_root=incoming,
            )

        if imported.rejected:
            return ProcessedReadySummary(
                batch_id=batch_id,
                completed=imported.completed,
                rejected=imported.rejected,
                next_batch_id=None,
            )

        state = self._load_state()
        state["last_processed_batch_id"] = batch_id
        state.pop("current", None)
        self._save_state(state)
        self.storage.delete(READY_POINTER)
        if transport == "archive":
            self.storage.delete(result_location)
            self.storage.delete(result_location + ".sha256")
        else:
            for name in remote_names:
                self.storage.delete(f"{result_location}/{name}")
            self.storage.delete(f"{result_location}/checksums.json")
        next_batch = self.ensure_current_batch()
        return ProcessedReadySummary(
            batch_id=batch_id,
            completed=imported.completed,
            rejected=0,
            next_batch_id=None if next_batch is None else next_batch.batch_id,
        )
