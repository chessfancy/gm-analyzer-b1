"""Deepnote manual batch execution cycle using project storage as the handoff."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

from chessgrandmaster.engine_manifest import resolve_installed_engine

from .manual_batch import safe_extract_zip
from .deepnote_manual_bridge import (
    CURRENT_ARCHIVE, CURRENT_POINTER, READY_POINTER, batch_archive_path,
)


@dataclass(frozen=True)
class DeepnoteCycleSummary:
    batch_id: str
    executed: int
    skipped: int
    results: int
    result_root: Path
    checksums_sha256: str


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


def _validated_archive_path(batch_id: str, value: object) -> str:
    archive_rel = str(value or CURRENT_ARCHIVE)
    if "\\" in archive_rel:
        raise ValueError(f"unexpected Deepnote batch archive path: {archive_rel}")
    path = PurePosixPath(archive_rel)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unexpected Deepnote batch archive path: {archive_rel}")
    expected = batch_archive_path(batch_id)
    if archive_rel not in {CURRENT_ARCHIVE, expected}:
        raise ValueError(f"unexpected Deepnote batch archive path: {archive_rel}")
    return archive_rel


def ensure_deepnote_runtime_ready(*, repo_root: str | Path | None = None) -> Path:
    """Ensure the managed Stockfish binary exists in a fresh Deepnote runtime."""

    try:
        return Path(resolve_installed_engine())
    except FileNotFoundError:
        pass

    repo = (
        Path(repo_root).expanduser().resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[3]
    )
    setup = repo / "scripts/setup_platform.sh"
    if not setup.is_file():
        raise FileNotFoundError(f"Deepnote setup script not found: {setup}")

    env = os.environ.copy()
    env["CGM_VENV"] = str(Path(sys.prefix))
    env["CGM_BOOTSTRAP_PYTHON"] = str(Path(sys.executable))
    env["CGM_INSTALL_DEV"] = "0"
    if importlib.util.find_spec("chess") is not None:
        env["CGM_SKIP_PACKAGE_INSTALL"] = "1"

    subprocess.run(["bash", str(setup)], cwd=repo, env=env, check=True)
    return Path(resolve_installed_engine())


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
    archive_rel = _validated_archive_path(batch_id, current.get("archive_path"))
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

    if executor is None:
        ensure_deepnote_runtime_ready()
    summary = _load_manual_runner()(batch_dir, result_dir, executor=executor)

    # Result files already live in persistent Deepnote project storage. Do not
    # recompress SQLite + already-gzipped raw-UCI into one giant ZIP. Publish a
    # tiny pointer last; Oracle reconstructs and verifies the tree file-by-file.
    checksums_path = result_dir / "checksums.json"
    if not checksums_path.is_file():
        raise RuntimeError(f"manual result tree has no checksums.json: {result_dir}")
    checksums_sha = _sha256(checksums_path)
    result_root_rel = result_dir.relative_to(work_root).as_posix()
    ready = {
        "schema_version": "cgm-deepnote-ready-2",
        "transport": "project-tree",
        "batch_id": batch_id,
        "result_root": result_root_rel,
        "checksums_path": f"{result_root_rel}/checksums.json",
        "checksums_sha256": checksums_sha,
        "results": summary.results,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(work_root / READY_POINTER, ready)
    return DeepnoteCycleSummary(
        batch_id=batch_id,
        executed=summary.executed,
        skipped=summary.skipped,
        results=summary.results,
        result_root=result_dir,
        checksums_sha256=checksums_sha,
    )
