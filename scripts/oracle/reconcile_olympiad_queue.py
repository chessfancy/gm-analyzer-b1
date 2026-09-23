#!/usr/bin/env python3
"""Keep the Olympiad coordinator queue on latest non-empty registered revisions."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3


CGM = Path("/home/ubuntu/data/cgm")
DB = CGM / "coordinator.sqlite"
FINISHED_STATE = CGM / "state/lichess-olympiad-finished.json"
OLYMPIAD_MARKER = "46th-fide-chess-olympiad-samarkand-2026"
PRODUCTION_MIN_PRIORITY = 500
STALE_PRIORITY = -10000
MAX_RETRY_ATTEMPTS = 5
ROUND_RE = re.compile(r"/event:round-(\d+)/")


def _round_number(tournament_id: str) -> int:
    match = ROUND_RE.search(tournament_id)
    return int(match.group(1)) if match else 0


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB) as connection:
        connection.row_factory = sqlite3.Row
        rows = list(
            connection.execute(
                "SELECT job_id, job_json, state, priority FROM jobs"
            )
        )

        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            job = json.loads(row["job_json"])
            tournament_id = str(job.get("tournament_id", ""))
            if OLYMPIAD_MARKER not in tournament_id:
                continue
            grouped[tournament_id].append(
                {
                    "job_id": row["job_id"],
                    "state": row["state"],
                    "priority": row["priority"],
                    "revision": int(job.get("tournament_revision", 0)),
                    "plies": int((job.get("input") or {}).get("plies") or 0),
                }
            )

        finished_state = {"processed": {}}
        if FINISHED_STATE.exists():
            finished_state = json.loads(
                FINISHED_STATE.read_text(encoding="utf-8")
            )
        allowed_revisions = {
            str(meta["tournament_id"]): int(meta["revision"])
            for meta in finished_state.get("processed", {}).values()
            if isinstance(meta, dict)
            and meta.get("tournament_id")
            and meta.get("revision") is not None
        }

        promoted = 0
        demoted = 0
        requeued = 0
        latest_revisions: dict[str, int | None] = {}

        for tournament_id, jobs in grouped.items():
            allowed = allowed_revisions.get(tournament_id)
            latest_revisions[tournament_id] = allowed
            priority = PRODUCTION_MIN_PRIORITY + 100 + _round_number(
                tournament_id
            )

            for job in jobs:
                eligible = (
                    allowed is not None
                    and job["revision"] == allowed
                    and job["plies"] > 0
                )
                if eligible:
                    if job["priority"] != priority:
                        connection.execute(
                            "UPDATE jobs SET priority=?, updated_at=? "
                            "WHERE job_id=?",
                            (priority, now, job["job_id"]),
                        )
                        promoted += 1

                    if job["state"] == "FAILED":
                        attempts = connection.execute(
                            "SELECT COUNT(*) FROM attempts WHERE job_id=?",
                            (job["job_id"],),
                        ).fetchone()[0]
                        if attempts < MAX_RETRY_ATTEMPTS:
                            connection.execute(
                                "UPDATE jobs SET state='RETRY_PENDING', "
                                "updated_at=? WHERE job_id=?",
                                (now, job["job_id"]),
                            )
                            requeued += 1
                elif job["state"] in {
                    "PENDING",
                    "RETRY_PENDING",
                    "FAILED",
                } and job["priority"] != STALE_PRIORITY:
                    connection.execute(
                        "UPDATE jobs SET priority=?, updated_at=? "
                        "WHERE job_id=?",
                        (STALE_PRIORITY, now, job["job_id"]),
                    )
                    demoted += 1

        connection.commit()

    summary = {
        "ok": True,
        "tournaments": len(grouped),
        "promoted": promoted,
        "demoted": demoted,
        "requeued": requeued,
        "production_min_priority": PRODUCTION_MIN_PRIORITY,
        "latest_revisions": latest_revisions,
    }
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
