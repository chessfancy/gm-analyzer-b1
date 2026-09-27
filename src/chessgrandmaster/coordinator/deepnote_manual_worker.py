"""Deepnote manual batch execution cycle using project storage as the handoff."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from .manual_batch import archive_directory, safe_extract_zip
from .deepnote_manual_bridge import CURRENT_ARCHIVE, CURRENT_POINTER, READY_POINTER

@dataclass(frozen=True)
class DeepnoteCycleSummary:
    batch_id: str
    executed: int
    skipped: int
    results: int
    archive: Path
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temp.replace(path)


def _load_manual_runner():
    from importlib.util import module_from_spec, spec_from_file_location
    script = Path(__file__).resolve().parents[3] / "scripts/workers/run_manual_batch.py"
    spec = spec_from_file_location("cgm_run_manual_batch", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {script}")
    module = module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.run_manual_batch


def run_deepnote_manual_cycle(
    *,
    work_root: str | Path = "/work",
    executor=None,
) -> DeepnoteCycleSummary:
    work_root = Path(work_root).expanduser().resolve()
    pointer_path = work_root / CURRENT_POINTER
    if not pointer_path.is_file():
        raise FileNotFoundError(f"Deepnote current batch pointer not found: {pointer_path}")
    current = json.loads(pointer_path.read_text(encoding="utf-8"))
    if current.get("schema_version") != "cgm-deepnote-current-1":
        raise ValueError("invalid Deepnote current batch pointer schema")
    batch_id = str(current.get("batch_id") or "")
    if not batch_id:
        raise ValueError("Deepnote current batch pointer has no batch_id")
    archive_rel = str(current.get("archive_path") or CURRENT_ARCHIVE)
    if archive_rel != CURRENT_ARCHIVE:
        raise ValueError(f"unexpected Deepnote batch archive path: {archive_rel}")
    batch_archive = work_root / archive_rel
    expected_sha = str(current.get("sha256") or "").lower()
    actual_sha = _sha256(batch_archive)
    if expected_sha != actual_sha:
        raise ValueError(f"Deepnote batch SHA256 mismatch: expected {expected_sha}, got {actual_sha}")

    runtime = work_root / "cgm-manual/runtime" / batch_id
    batch_dir = runtime / "batch"
    result_dir = runtime / "result"
    if batch_dir.exists():
        shutil.rmtree(batch_dir)
    safe_extract_zip(batch_archive, batch_dir)
    batch_payload = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    if str(batch_payload.get("batch_id")) != batch_id:
        raise ValueError("Deepnote current pointer batch_id does not match batch archive")

    summary = _load_manual_runner()(batch_dir, result_dir, executor=executor)
    outbox = work_root / "cgm-manual/outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    result_archive = archive_directory(result_dir, outbox / f"{batch_id}.zip")
    result_sha = _sha256(result_archive)
    result_archive.with_suffix(".zip.sha256").write_text(
        f"{result_sha}  {result_archive.name}\n", encoding="utf-8"
    )
    ready = {
        "schema_version": "cgm-deepnote-ready-1",
        "batch_id": batch_id,
        "archive_path": f"cgm-manual/outbox/{result_archive.name}",
        "sha256": result_sha,
        "bytes": result_archive.stat().st_size,
        "results": summary.results,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(work_root / READY_POINTER, ready)
    return DeepnoteCycleSummary(
        batch_id=batch_id,
        executed=summary.executed,
        skipped=summary.skipped,
        results=summary.results,
        archive=result_archive,
        sha256=result_sha,
    )

