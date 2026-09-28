#!/usr/bin/env python3
"""Execute a coordinator-native manual batch and package portable results."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import sys

from chessgrandmaster.coordinator import validate_job_bundle, validate_result_bundle, write_checksums
from chessgrandmaster.coordinator.bundles import canonical_json_bytes
from chessgrandmaster.coordinator.manual_batch import _verify_tree_checksums, archive_directory, safe_extract_zip


@dataclass(frozen=True)
class ManualRunSummary:
    batch_id: str
    executed: int
    skipped: int
    results: int
    result_path: Path


def _load_job_executor():
    repo_root = Path(__file__).resolve().parents[2]
    for path in (repo_root / "src", repo_root, Path(__file__).resolve().parent):
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
    from scripts.workers.run_job_bundle import execute_job
    return execute_job


def _default_executor(bundle: Path, result: Path, provider: str) -> Path:
    return _load_job_executor()(bundle, result, provider)


def run_manual_batch(
    batch_dir: str | Path,
    result_dir: str | Path,
    *,
    executor=None,
    progress_callback=None,
) -> ManualRunSummary:
    batch_dir = Path(batch_dir).resolve()
    result_dir = Path(result_dir).resolve()
    _verify_tree_checksums(batch_dir)
    batch = json.loads((batch_dir / "batch.json").read_text(encoding="utf-8"))
    if batch.get("schema_version") != "cgm-manual-batch-1":
        raise ValueError("invalid manual batch schema")
    runtime_provider = str(batch["runtime_provider"])
    execute = executor or _default_executor
    results_root = result_dir / "results"
    results_root.mkdir(parents=True, exist_ok=True)
    executed = 0
    skipped = 0
    result_items: list[dict[str, object]] = []
    jobs = list(batch.get("jobs", []))
    total_jobs = len(jobs)

    for item in jobs:
        ordinal = int(item["ordinal"])
        bundle = batch_dir / str(item["path"])
        expected = validate_job_bundle(bundle)
        result = results_root / f"{ordinal:04d}"
        if result.is_dir():
            try:
                validate_result_bundle(result, expected_job=expected)
            except Exception:
                shutil.rmtree(result)
            else:
                skipped += 1
                result_items.append({"ordinal": ordinal, "job_id": expected.job_id, "path": f"results/{ordinal:04d}"})
                if progress_callback is not None:
                    source = json.loads((bundle / "job.json").read_text(encoding="utf-8")).get("input") or {}
                    progress_callback({
                        "completed_jobs": len(result_items), "total_jobs": total_jobs,
                        "ordinal": ordinal, "job_id": expected.job_id,
                        "games": int(source.get("games") or 0), "plies": int(source.get("plies") or 0),
                        "executed": executed, "skipped": skipped, "was_skipped": True,
                    })
                continue
        execute(bundle, result, runtime_provider)
        validate_result_bundle(result, expected_job=expected)
        executed += 1
        result_items.append({"ordinal": ordinal, "job_id": expected.job_id, "path": f"results/{ordinal:04d}"})
        if progress_callback is not None:
            source = json.loads((bundle / "job.json").read_text(encoding="utf-8")).get("input") or {}
            progress_callback({
                "completed_jobs": len(result_items), "total_jobs": total_jobs,
                "ordinal": ordinal, "job_id": expected.job_id,
                "games": int(source.get("games") or 0), "plies": int(source.get("plies") or 0),
                "executed": executed, "skipped": skipped, "was_skipped": False,
            })

    payload = {
        "schema_version": "cgm-manual-result-batch-1",
        "batch_id": batch["batch_id"],
        "runtime_provider": runtime_provider,
        "results": result_items,
    }
    (result_dir / "batch-result.json").write_bytes(canonical_json_bytes(payload))
    write_checksums(result_dir)
    return ManualRunSummary(
        batch_id=str(batch["batch_id"]),
        executed=executed,
        skipped=skipped,
        results=len(result_items),
        result_path=result_dir,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--batch", type=Path)
    group.add_argument("--batch-archive", type=Path)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    batch_dir = args.batch
    if args.batch_archive is not None:
        batch_dir = args.result.parent / ".manual-batch-input"
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
        safe_extract_zip(args.batch_archive, batch_dir)
    assert batch_dir is not None
    summary = run_manual_batch(batch_dir, args.result)
    archive = archive_directory(args.result, args.archive)
    sha = _sha256(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(f"{sha}  {archive.name}\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "batch_id": summary.batch_id,
        "executed": summary.executed,
        "skipped": summary.skipped,
        "results": summary.results,
        "archive": str(archive),
        "sha256": sha,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
