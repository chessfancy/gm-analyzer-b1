# Deepnote manual bridge

Deepnote manual analysis uses Deepnote project storage as the transport. No Oracle credential is stored in the notebook.

## Lifecycle

1. Oracle leases coordinator jobs into a `cgm-manual-batch-1` archive.
2. Oracle uploads the archive to `/work/cgm-manual/inbox/current-batch.zip` and writes `current.json` last.
3. The user loads the current batch in Deepnote and starts analysis.
4. `scripts/workers/run_deepnote_manual_cycle.py` executes every immutable job bundle, resumes already-valid results, writes one result ZIP, then writes `/work/cgm-manual/outbox/ready.json` last.
5. Oracle polls `ready.json`, downloads and SHA-verifies the ZIP, imports every result through `Coordinator.import_result()`, removes the consumed outbox files, then publishes the next batch.

## Deepnote load cell

```python
from pathlib import Path
import hashlib, json

root = Path("/work/cgm-manual/inbox")
current = json.loads((root / "current.json").read_text(encoding="utf-8"))
archive = root / "current-batch.zip"
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

The Oracle bridge cron handles the rest. The result ZIP stays in Deepnote project storage until Oracle has verified and imported it.
