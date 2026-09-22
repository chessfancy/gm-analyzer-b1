#!/usr/bin/env python3
"""Execute one immutable cgm-job-1 bundle and build a verified result bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

from chessgrandmaster.b2.manifests import canonical_json_bytes
from chessgrandmaster.coordinator import validate_job_bundle, write_checksums
from chessgrandmaster.coordinator.profiles import get_provider_profile
from chessgrandmaster.production_pipeline import run_pipeline


def _single(pattern: str, root: Path) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {pattern!r} under {root}, got {matches}")
    return matches[0]


def _analysis_value(analysis: dict, key: str, default):
    value = analysis.get(key, default)
    return default if value is None else value


def execute_job(bundle: Path, result_root: Path, provider: str) -> Path:
    validated = validate_job_bundle(bundle)
    job = validated.job_json
    analysis = dict(job.get("analysis") or {})
    profile = get_provider_profile(provider)

    result_root = result_root.resolve()
    if result_root.exists():
        shutil.rmtree(result_root)
    result_root.mkdir(parents=True)

    work_root = result_root.parent / f".{result_root.name}-work"
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)

    summary = run_pipeline(
        validated.path / "input.pgn",
        root=work_root,
        workers=profile.workers,
        threads=profile.threads,
        hash_mb=profile.hash_mb,
        multipv=int(_analysis_value(analysis, "multipv", 1)),
        depth=int(_analysis_value(analysis, "depth", profile.depth)),
        time_sec=float(_analysis_value(analysis, "time_sec", 0.0)),
        snapshot_depths=tuple(
            int(value)
            for value in _analysis_value(
                analysis, "snapshot_depths", (12, 14, 16, 18, 19)
            )
        ),
    )

    database = Path(summary["database"])
    shutil.copy2(database, result_root / "analysis.sqlite")

    output_dir = work_root / "output"
    shutil.copy2(_single("Mistakes_*.pgn", output_dir), result_root / "Mistakes.pgn")
    shutil.copy2(_single("Blunders_*.pgn", output_dir), result_root / "Blunders.pgn")

    uci_root = work_root / "uci"
    if not uci_root.is_dir() or not any(uci_root.rglob("*.jsonl.gz")):
        raise RuntimeError("B1 produced no raw UCI archive")
    shutil.copytree(uci_root, result_root / "raw-uci")

    input_obj = dict(job.get("input") or {})
    result_json = {
        "schema_version": "cgm-job-result-1",
        "job_id": validated.job_id,
        "config_hash": validated.config_hash,
        "tournament_id": job.get("tournament_id"),
        "tournament_revision": job.get("tournament_revision"),
        "shard_index": job.get("shard_index"),
        "input": {
            "key": input_obj.get("key"),
            "sha256": validated.input_identity["input_sha256"],
            "canonical_game_fingerprints": list(
                input_obj.get("canonical_game_fingerprints") or ()
            ),
        },
        "runtime": {
            "provider": profile.name,
            "workers": profile.workers,
            "threads": profile.threads,
            "hash_mb": profile.hash_mb,
        },
        "summary": {
            "run_id": summary.get("run_id"),
            "games": summary.get("games"),
            "moves": summary.get("moves"),
            "mistakes": summary.get("mistakes"),
            "blunders": summary.get("blunders"),
        },
    }
    (result_root / "job-result.json").write_bytes(canonical_json_bytes(result_json))
    write_checksums(result_root)
    return result_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument(
        "--provider",
        choices=("kaggle", "deepnote", "molab-marimo", "codespaces", "oracle-urgent"),
        required=True,
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    out = execute_job(args.bundle, args.result, args.provider)
    print(json.dumps({"ok": True, "result_bundle": str(out)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
