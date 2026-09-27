"""Portable manual batch transport for Deepnote and Molab workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import uuid
import zipfile

from .bundles import (
    BundleValidationError,
    canonical_json_bytes,
    compute_checksums,
    validate_job_bundle,
    write_checksums,
)
from .profiles import get_provider_profile
from .queue import Coordinator


@dataclass(frozen=True)
class ManualBatchSummary:
    batch_id: str
    provider: str
    runtime_provider: str
    jobs: int
    path: Path


@dataclass(frozen=True)
class ManualImportSummary:
    completed: int
    rejected: int
    job_ids: tuple[str, ...]
    rejected_jobs: tuple[str, ...]
    extracted_path: Path


def _manual_provider(runtime_provider: str) -> tuple[str, str]:
    label = "molab" if runtime_provider == "molab-marimo" else runtime_provider
    return f"{label}-manual-batch", f"{label}-manual"


def _verify_tree_checksums(root: Path) -> None:
    path = root / "checksums.json"
    if not path.is_file():
        raise ValueError(f"manual archive has no checksums.json: {root}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "cgm-checksums-1":
        raise ValueError("manual archive has invalid checksum schema")
    expected = payload.get("files")
    if not isinstance(expected, dict) or expected != compute_checksums(root):
        raise ValueError("manual archive checksum mismatch")


def create_manual_batch(
    *,
    coordinator: Coordinator,
    destination: str | Path,
    provider: str,
    max_jobs: int,
    min_priority: int | None = 500,
    max_plies: int | None = None,
    lease_seconds: int | float = 14 * 24 * 3600,
) -> ManualBatchSummary:
    if max_jobs < 1:
        raise ValueError("max_jobs must be positive")
    destination = Path(destination).expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"manual batch destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    runtime_provider = get_provider_profile(provider).name
    attempt_provider, worker = _manual_provider(runtime_provider)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_id = f"{attempt_provider}-{stamp}-{uuid.uuid4().hex[:8]}"
    jobs: list[dict[str, object]] = []

    for ordinal in range(max_jobs):
        lease = coordinator.lease_next(
            worker,
            attempt_provider,
            lease_seconds=lease_seconds,
            min_priority=min_priority,
            max_plies=max_plies,
        )
        if lease is None:
            break
        relative = Path("jobs") / f"{ordinal:04d}"
        exported = coordinator.export_job(lease.job_id, destination / relative)
        validated = validate_job_bundle(exported)
        jobs.append(
            {
                "ordinal": ordinal,
                "job_id": lease.job_id,
                "attempt_id": lease.attempt_id,
                "attempt_number": lease.attempt_number,
                "path": relative.as_posix(),
                "input_sha256": validated.input_identity["input_sha256"],
                "config_hash": validated.config_hash,
            }
        )

    payload = {
        "schema_version": "cgm-manual-batch-1",
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "runtime_provider": runtime_provider,
        "attempt_provider": attempt_provider,
        "jobs": jobs,
    }
    (destination / "batch.json").write_bytes(canonical_json_bytes(payload))
    write_checksums(destination)
    return ManualBatchSummary(
        batch_id=batch_id,
        provider=provider,
        runtime_provider=runtime_provider,
        jobs=len(jobs),
        path=destination,
    )


def archive_directory(root: str | Path, archive_path: str | Path) -> Path:
    root = Path(root).resolve()
    archive_path = Path(archive_path).expanduser().resolve()
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp = archive_path.with_name(f".{archive_path.name}.{uuid.uuid4().hex}.tmp")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            handle.write(path, path.relative_to(root).as_posix())
    temp.replace(archive_path)
    return archive_path


def safe_extract_zip(archive_path: str | Path, destination: str | Path) -> Path:
    archive_path = Path(archive_path).resolve()
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as handle:
        for info in handle.infolist():
            name = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if name.is_absolute() or ".." in name.parts or stat.S_ISLNK(mode):
                raise ValueError(f"unsafe ZIP member: {info.filename}")
            target = (destination / Path(*name.parts)).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"unsafe ZIP member: {info.filename}")
        handle.extractall(destination)
    return destination


def import_manual_result_archive(
    *,
    coordinator: Coordinator,
    archive_path: str | Path,
    extract_root: str | Path,
) -> ManualImportSummary:
    archive_path = Path(archive_path).resolve()
    extract_root = Path(extract_root).resolve()
    extract_root.mkdir(parents=True, exist_ok=True)
    extracted = Path(tempfile.mkdtemp(prefix=f"{archive_path.stem}-", dir=extract_root))
    safe_extract_zip(archive_path, extracted)
    _verify_tree_checksums(extracted)
    result_manifest = extracted / "batch-result.json"
    if not result_manifest.is_file():
        raise ValueError("manual result archive has no batch-result.json")
    payload = json.loads(result_manifest.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "cgm-manual-result-batch-1":
        raise ValueError("manual result archive has invalid schema")

    completed: list[str] = []
    rejected: list[str] = []
    results_root = extracted / "results"
    for result in sorted(path for path in results_root.iterdir() if path.is_dir()):
        try:
            receipt = coordinator.import_result(result)
        except (BundleValidationError, ValueError, OSError):
            job_id = "unknown"
            try:
                job_id = str(json.loads((result / "job-result.json").read_text())["job_id"])
            except Exception:
                pass
            rejected.append(job_id)
            continue
        completed.append(receipt.job_id)

    return ManualImportSummary(
        completed=len(completed),
        rejected=len(rejected),
        job_ids=tuple(completed),
        rejected_jobs=tuple(rejected),
        extracted_path=extracted,
    )
