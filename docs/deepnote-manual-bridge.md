# Deepnote manual bridge

Deepnote manual analysis uses Deepnote project storage as the transport. No Oracle credential is stored in the notebook.

## Lifecycle

1. Oracle leases coordinator jobs into a `cgm-manual-batch-1` archive.
2. Oracle uploads the immutable archive to `/work/cgm-manual/inbox/batches/<batch_id>.zip` and writes `current.json` last.
3. The user starts analysis in Deepnote; the worker reads `archive_path` from `current.json`.
4. `scripts/workers/run_deepnote_manual_cycle.py` executes/resumes every immutable job bundle and updates `progress.json` after each completed shard.
5. Results stay in `/work/cgm-manual/runtime/<batch_id>/result`; `ready.json` publishes the checksum-verified project-tree handoff last.
6. Oracle verifies every file listed in the checksum manifest, imports each result through `Coordinator.import_result()`, cleans the consumed result tree, then publishes the next batch.

## Deepnote load cell

```python
from pathlib import Path
import hashlib, json

work = Path("/work")
pointer = work / "cgm-manual/inbox/current.json"
current = json.loads(pointer.read_text(encoding="utf-8"))
archive = work / current["archive_path"]
if not archive.is_file():
    raise FileNotFoundError(f"Deepnote batch archive missing: {archive}")
actual = hashlib.sha256(archive.read_bytes()).hexdigest()
if actual != current["sha256"]:
    raise RuntimeError(f"batch SHA mismatch: {actual} != {current['sha256']}")
print("READY", current["batch_id"], "jobs=", current["jobs"], "games=", current["games"], "plies=", current["plies"])
```

## Deepnote analyze cell

This replaces the old aggregate-PGN analysis cell for coordinator batches.

```python
from pathlib import Path
import os, subprocess, sys

candidates = [Path("/work/chessgrandmaster"), Path("/work/gm-analyzer-b1"), Path.cwd()]
repo = next((p for p in candidates if (p / "scripts/workers/run_deepnote_manual_cycle.py").is_file()), None)
if repo is None:
    raise RuntimeError("gm-analyzer-b1 repo not found")

env = os.environ.copy()
env["PYTHONPATH"] = str(repo / "src")
subprocess.run(
    [sys.executable, str(repo / "scripts/workers/run_deepnote_manual_cycle.py"), "--work-root", "/work"],
    cwd=repo,
    env=env,
    check=True,
)
print("RESULT READY FOR ORACLE")
```

The Oracle bridge cron handles the rest. Results stay in the persistent project tree until Oracle has checksum-verified and imported them.

## Progress heartbeat

From the heartbeat-enabled worker revision onward, Deepnote atomically updates:

```text
/work/cgm-manual/runtime/<batch_id>/progress.json
```

after every completed or resumed shard. The heartbeat reports batch state, total/completed jobs, games and plies, executed/skipped counts, the last completed job, and `updated_at`. This is intended for Oracle-side monitoring when notebook stdout is buffered. A batch that started on an older revision will not gain a heartbeat mid-run; the next run that pulls the heartbeat revision will.
