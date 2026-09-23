#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import urllib.request

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
STATE = CGM / "state/lichess-olympiad-finished.json"

BROADCAST_ROOT = (
    "https://lichess.org/broadcast/"
    "46th-fide-chess-olympiad-samarkand-2026/32sSzO6S"
)
TARGET_PLIES = 1800
PRODUCTION_PRIORITY_BASE = 700
ROUND_RE = re.compile(r"/round-(\d+)/")


def run_json(
    command: list[str],
    *,
    timeout: int = 240,
    attempts: int = 3,
) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(
            command,
            cwd=REPO,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                last_error = RuntimeError(
                    f"command returned non-JSON output: {proc.stdout[-4000:]}"
                )
        else:
            last_error = RuntimeError(
                f"command failed rc={proc.returncode}: {' '.join(command)}\n"
                f"stdout={proc.stdout[-4000:]}\nstderr={proc.stderr[-4000:]}"
            )
        if attempt < attempts:
            time.sleep(2 * attempt)
    assert last_error is not None
    raise last_error


def _fetch_text(url: str, *, attempts: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "ChessGrandmaster acquisition/1.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read().decode("utf-8", "replace")
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
    assert last_error is not None
    raise last_error


def _extract_rounds_from_html(html: str) -> list[dict]:
    marker = '"rounds":'
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        index = html.find(marker, cursor)
        if index < 0:
            raise ValueError("Lichess page contains no rounds payload")
        try:
            value, _end = decoder.raw_decode(html[index + len(marker) :])
        except json.JSONDecodeError:
            cursor = index + len(marker)
            continue
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        cursor = index + len(marker)


def _finished_round_urls(source_url: str) -> list[dict]:
    rounds = _extract_rounds_from_html(_fetch_text(source_url))
    output: list[dict] = []
    for item in rounds:
        url = item.get("url")
        if item.get("finished") is not True or not isinstance(url, str):
            continue
        if not url.startswith("https://lichess.org/broadcast/"):
            continue
        output.append(
            {
                "url": url,
                "name": item.get("name"),
                "finished_at": item.get("finishedAt"),
            }
        )
    return output


def _load_state() -> dict:
    if not STATE.exists():
        return {"processed": {}}
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"processed": {}}
    if not isinstance(value, dict) or not isinstance(value.get("processed"), dict):
        return {"processed": {}}
    return value


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(STATE)


def _registry_identity(registry_tournament_id: int) -> tuple[str, int]:
    with sqlite3.connect(REGISTRY) as connection:
        row = connection.execute(
            "SELECT slug FROM tournaments WHERE id = ?",
            (registry_tournament_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"registry tournament {registry_tournament_id} disappeared"
            )
        revision = connection.execute(
            "SELECT MAX(revision_number) FROM tournament_revisions "
            "WHERE tournament_id = ?",
            (registry_tournament_id,),
        ).fetchone()[0]
    if revision is None:
        raise RuntimeError(
            f"registry tournament {registry_tournament_id} has no revision"
        )
    return str(row[0]), int(revision)


def _coordinator_has_revision(tournament_id: str, revision: int) -> bool:
    if not COORDINATOR_DB.exists():
        return False
    with sqlite3.connect(COORDINATOR_DB) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM jobs
            WHERE json_extract(job_json, '$.tournament_id') = ?
              AND CAST(
                    json_extract(job_json, '$.tournament_revision')
                    AS INTEGER
                  ) = ?
            LIMIT 1
            """,
            (tournament_id, revision),
        ).fetchone()
    return row is not None


def _round_number(url: str) -> int:
    match = ROUND_RE.search(url)
    return int(match.group(1)) if match else 0


def _register_revision(
    package: dict,
    *,
    priority: int,
) -> list[str]:
    tournament_id = str(package["tournament_id"])
    revision = int(package["revision"])
    shard_root = (
        PACKAGE_WORK
        / tournament_id
        / f"{revision:04d}"
        / "shards"
    )
    if not shard_root.is_dir():
        raise RuntimeError(f"missing package shard directory: {shard_root}")

    coordinator = Coordinator(COORDINATOR_DB, archive_root=ARCHIVE)
    registered: list[str] = []
    for bundle in sorted(path for path in shard_root.iterdir() if path.is_dir()):
        if not (bundle / "job.json").is_file():
            continue
        handle = coordinator.register_job(bundle, priority=priority)
        registered.append(handle.job_id)
    if not registered:
        raise RuntimeError(f"no job bundles under {shard_root}")
    return registered


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    PACKAGES.mkdir(parents=True, exist_ok=True)

    adapter = LichessBroadcastAdapter(timeout_sec=25)
    current_refs = adapter.discover(BROADCAST_ROOT)
    current_urls = sorted({ref.source_url for ref in current_refs})

    finished: dict[str, dict] = {}
    discovery_errors: list[dict] = []
    for source_url in current_urls:
        try:
            for item in _finished_round_urls(source_url):
                finished[item["url"]] = item
        except Exception as exc:
            discovery_errors.append(
                {"source_url": source_url, "error": str(exc)}
            )

    state = _load_state()
    processed: dict = state.setdefault("processed", {})
    results: list[dict] = []
    errors: list[dict] = []

    def sort_key(item: tuple[str, dict]) -> tuple[int, str]:
        url, _meta = item
        return (_round_number(url), url)

    for source_url, meta in sorted(finished.items(), key=sort_key):
        if source_url in processed:
            results.append(
                {
                    "source_url": source_url,
                    "status": "already_processed",
                    **meta,
                }
            )
            continue

        try:
            acquired = run_json(
                [
                    str(VENV / "cgm-acquire"),
                    source_url,
                    "--registry",
                    str(REGISTRY),
                    "--root",
                    str(CORPUS),
                ]
            )
            if not acquired.get("ok"):
                raise RuntimeError(f"acquisition failed: {acquired}")

            registry_tournament_id = int(acquired["tournament"]["id"])
            tournament_id, revision = _registry_identity(
                registry_tournament_id
            )
            canonical = acquired.get("canonical") or {}
            plies = int(canonical.get("ply_count") or 0)
            games = int(canonical.get("game_count") or 0)
            if plies <= 0:
                raise RuntimeError(
                    f"finished Lichess round has no moves: {source_url}"
                )

            priority = PRODUCTION_PRIORITY_BASE + _round_number(source_url)
            if _coordinator_has_revision(tournament_id, revision):
                registered: list[str] = []
                status = "already_registered"
            else:
                packaged = run_json(
                    [
                        str(VENV / "cgm-package"),
                        str(registry_tournament_id),
                        "--registry",
                        str(REGISTRY),
                        "--root",
                        str(PACKAGES),
                        "--target-plies",
                        str(TARGET_PLIES),
                    ]
                )
                registered = _register_revision(
                    packaged,
                    priority=priority,
                )
                status = "registered"

            processed[source_url] = {
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "tournament_id": tournament_id,
                "revision": revision,
                "games": games,
                "plies": plies,
            }
            _save_state(state)
            results.append(
                {
                    "source_url": source_url,
                    "status": status,
                    "tournament_id": tournament_id,
                    "revision": revision,
                    "games": games,
                    "plies": plies,
                    "registered_jobs": registered,
                    **meta,
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "source_url": source_url,
                    "error": str(exc),
                    **meta,
                }
            )

    now = datetime.now(timezone.utc)
    report = {
        "ok": not discovery_errors,
        "run_at_utc": now.isoformat(),
        "broadcast_root": BROADCAST_ROOT,
        "current_feeds": len(current_urls),
        "finished_round_feeds": len(finished),
        "processed_this_run": sum(
            1 for item in results if item["status"] != "already_processed"
        ),
        "already_processed": sum(
            1 for item in results if item["status"] == "already_processed"
        ),
        "errors": errors,
        "discovery_errors": discovery_errors,
        "results": results,
    }
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS / f"lichess-olympiad-{stamp}.json"
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "report": str(path),
                "current_feeds": len(current_urls),
                "finished_round_feeds": len(finished),
                "processed_this_run": report["processed_this_run"],
                "errors": len(errors),
            },
            sort_keys=True,
        )
    )
    return 0 if not discovery_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
