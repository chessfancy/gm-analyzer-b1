#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from chessgrandmaster.b2.sources.lichess_broadcast import LichessBroadcastAdapter
from chessgrandmaster.coordinator import Coordinator

REPO = Path("/home/ubuntu/projects/gm-analyzer-b1")
VENV = REPO / ".venv" / "bin"
CGM = Path("/home/ubuntu/data/cgm")
CORPUS = CGM / "corpus-2026"
REGISTRY = CORPUS / "registry.sqlite"
PACKAGES = CGM / "packages"
PACKAGE_WORK = CGM / ".packages.cgm-package-work"
COORDINATOR_DB = CGM / "coordinator.sqlite"
ARCHIVE = CGM / "archive"
REPORTS = CGM / "reports"

BROADCAST_ROOT = (
    "https://lichess.org/broadcast/"
    "46th-fide-chess-olympiad-samarkand-2026/32sSzO6S"
)
def run_json(command: list[str], *, timeout: int = 180) -> dict:
    proc = subprocess.run(
        command,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed rc={proc.returncode}: {' '.join(command)}\n"
            f"stdout={proc.stdout[-4000:]}\nstderr={proc.stderr[-4000:]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"command returned non-JSON output: {proc.stdout[-4000:]}"
        ) from exc


def register_packages() -> list[str]:
    coordinator = Coordinator(COORDINATOR_DB, archive_root=ARCHIVE)
    registered: list[str] = []
    if not PACKAGE_WORK.exists():
        return registered
    for checksum_path in sorted(PACKAGE_WORK.rglob("checksums.json")):
        bundle = checksum_path.parent
        handle = coordinator.register_job(bundle, priority=100)
        registered.append(handle.job_id)
    return registered


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    PACKAGES.mkdir(parents=True, exist_ok=True)

    adapter = LichessBroadcastAdapter(timeout_sec=25)
    refs = adapter.discover(BROADCAST_ROOT)

    unique = {}
    for ref in refs:
        unique[ref.external_id] = ref

    results: list[dict] = []
    for ref in sorted(unique.values(), key=lambda r: r.external_id):
        acquired = run_json([
            str(VENV / "cgm-acquire"),
            ref.source_url,
            "--registry", str(REGISTRY),
            "--root", str(CORPUS),
        ])
        if not acquired.get("ok"):
            raise RuntimeError(f"acquisition failed: {acquired}")

        tournament_id = str(acquired["tournament"]["id"])
        packaged = run_json([
            str(VENV / "cgm-package"),
            tournament_id,
            "--registry", str(REGISTRY),
            "--root", str(PACKAGES),
            "--target-plies", "3000",
        ])
        results.append({
            "external_id": ref.external_id,
            "source_url": ref.source_url,
            "tournament_id": tournament_id,
            "acquisition": acquired,
            "package": packaged,
        })

    registered = register_packages()
    now = datetime.now(timezone.utc)
    report = {
        "ok": True,
        "run_at_utc": now.isoformat(),
        "broadcast_root": BROADCAST_ROOT,
        "feeds_discovered": len(refs),
        "feeds_unique": len(unique),
        "jobs_registered_total_scan": len(registered),
        "results": results,
    }
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS / f"lichess-olympiad-{stamp}.json"
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "ok": True,
        "report": str(path),
        "feeds": len(results),
        "registered_jobs": len(registered),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
