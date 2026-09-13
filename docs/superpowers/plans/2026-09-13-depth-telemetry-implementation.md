# Depth-Only Search and Engine Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make B1 depth-only, capture Stockfish D12/D14/D16/D18/D19 snapshots for primary and post-move searches, record runtime memory telemetry, and run Molab at 4 workers / 512 MB Hash / D19.

**Architecture:** Keep Lucas classification/export semantics unchanged. Replace each final `engine.analyse()` call with one streamed iterative search so intermediate depths are observed without restarting Stockfish. Carry snapshots/timestamps through the existing multiprocessing payload into SQLite. Add a dependency-free Linux `/proc` + cgroup sampler in the parent analyzer and persist samples at batch checkpoints.

**Tech Stack:** Python 3.11+, python-chess >=1.999, SQLite, multiprocessing, pytest, marimo.

**Spec:** `docs/superpowers/specs/2026-09-13-depth-telemetry-design.md`

## Global Constraints

- `time_sec=0.0` means no Stockfish time limit.
- Production depth must be positive.
- Snapshot checkpoints: `(12, 14, 16, 18, 19)`; ignore checkpoints above final depth.
- One iterative search per primary/post-move search; no repeated searches per checkpoint.
- Store engine WDL, CP/mate, PV, seldepth, nodes, NPS, time.
- Normalize post-move snapshots to original mover POV and prepend the played move.
- Molab test policy: workers=4, threads=1, hash=512 MB/worker, depth=19, time off.
- No benchmarks, auto-tuning, complexity score, TWIC work, or Lucas threshold/export changes.

---

### Task 1: Stream search and capture depth snapshots

**Files:**
- Modify: `src/chessgrandmaster/engine_worker.py`
- Create: `tests/test_depth_snapshots.py`

**Interfaces:**
- Add `DepthSnapshot` dataclass with `source_search`, `checkpoint_depth`, `reported_depth`, `uci`, `cp`, `mate`, WDL triplet, `seldepth`, `nodes`, `nps`, `time_ms`, `pv_uci`.
- Add `AnalysisResult.depth_snapshots`.
- Add `snapshot_depths=(12,14,16,18,19)` and default `time_sec=0.0` to `LucasEngineWorker`.

- [ ] Write RED tests:

```python
def test_depth_only_limit_omits_time():
    worker = object.__new__(LucasEngineWorker)
    worker.depth = 19
    worker.time_sec = 0.0
    worker.nodes = 0
    limit = worker._limit()
    assert limit.depth == 19
    assert limit.time is None


def test_active_checkpoints_stop_at_final_depth():
    worker = object.__new__(LucasEngineWorker)
    worker.depth = 18
    worker.snapshot_depths = (12, 14, 16, 18, 19)
    assert worker._active_snapshot_depths() == (12, 14, 16, 18)
```

Also test post-move snapshot normalization: played move prepended to PV, first UCI equals played move, CP/mate/WDL use original mover POV.

- [ ] Run `pytest -q tests/test_depth_snapshots.py`; expect FAIL.
- [ ] Add `DepthSnapshot`, active checkpoint helper, and configure Stockfish with `UCI_ShowWDL=True` alongside Threads/Hash.
- [ ] Implement `_stream_search(board, pov_color, source_search, forced_first_move=None)` using one `self.engine.analysis(...)` stream. Keep latest info by `multipv`; capture rank-1 snapshot once when each checkpoint depth is first reached; at stream end convert latest infos into final `EngineResponse` rows.
- [ ] Route both primary and post-move searches through `_stream_search()`. Preserve response sorting, played-rank, Lucas loss/category/NAG semantics.
- [ ] Run `pytest -q tests/test_depth_snapshots.py tests/golden/test_tre_2026_game_1.py`; expect PASS.
- [ ] Commit: `feat: capture iterative depth snapshots`.

---

### Task 2: Persist snapshots and real timestamps

**Files:**
- Modify: `src/chessgrandmaster/parallel_runner.py`
- Modify: `src/chessgrandmaster/analyzer.py`
- Modify: `src/chessgrandmaster/production_pipeline.py`
- Create: `tests/test_analysis_telemetry.py`

**Interfaces:**
- Worker result keys: `started_at`, `finished_at`, `depth_snapshots`.
- New SQLite table `engine_depth_snapshots` from the approved spec.
- `analysis_runs.created_at` and move start/finish timestamps become real UTC ISO values.

- [ ] Write a RED test that calls `ensure_schema()`, writes one synthetic result through `_write_results()`, and asserts timestamps plus D12/D14 snapshot rows are stored. Retry the same analysis and assert old snapshots are replaced, not duplicated.
- [ ] Run `pytest -q tests/test_analysis_telemetry.py`; expect FAIL.
- [ ] Add `engine_depth_snapshots` schema and index exactly as specified in the design.
- [ ] In `_worker_loop()`, capture UTC start/end around `analyze_move()` and serialize `depth_snapshots` with `asdict()`.
- [ ] In `_write_results()`, persist start/end times; on retry delete both `engine_responses` and `engine_depth_snapshots`; insert all snapshots.
- [ ] Add `snapshot_depths` to `TournamentAnalyzer` and `ParallelLucasRunner`, carry it to `LucasEngineWorker`.
- [ ] In `create_run()`, set `created_at`; write `time_limit_ms=NULL` when time is off.
- [ ] Run `pytest -q tests/test_analysis_telemetry.py tests/test_run_recovery.py`; expect PASS.
- [ ] Commit: `feat: persist depth telemetry and timestamps`.

---

### Task 3: Record runtime memory/process evidence

**Files:**
- Create: `src/chessgrandmaster/resource_telemetry.py`
- Modify: `src/chessgrandmaster/analyzer.py`
- Modify: `src/chessgrandmaster/production_pipeline.py`
- Create: `tests/test_resource_telemetry.py`

**Interfaces:**
- `collect_resource_sample(root_pid, configured_workers, configured_hash_mb_per_worker) -> dict`
- `resource_summary(db_path, run_id) -> dict`
- New table `runtime_resource_samples` from the approved spec.

- [ ] Write RED tests for `parse_meminfo()`, cgroup integer/`max` handling, synthetic `/proc` process-tree RSS, four Stockfish descendants, and summary aggregation.
- [ ] Run `pytest -q tests/test_resource_telemetry.py`; expect FAIL.
- [ ] Implement dependency-free `/proc` readers. Process rows contain `pid`, `ppid`, `name`, `cmdline`, `rss_bytes`; Stockfish is identified by process name or executable basename starting with `stockfish`.
- [ ] Collect `/sys/fs/cgroup/memory.current`, `memory.max`, `/proc/meminfo`, total B1 process-tree RSS, Stockfish count/RSS, and configured total Hash. Missing/unparseable values return `None`; telemetry failure must never fail chess analysis.
- [ ] Add `runtime_resource_samples` schema/index.
- [ ] Sample immediately after runner start, after every analysis batch, and once before runner close.
- [ ] Implement summary fields: sample count, total configured Hash, peak Stockfish RSS, peak B1 tree RSS, peak cgroup usage, cgroup limit, minimum MemAvailable, max Stockfish count.
- [ ] Print a `RESOURCE AUDIT` section after analysis.
- [ ] Run `pytest -q tests/test_resource_telemetry.py tests/test_analysis_telemetry.py`; expect PASS.
- [ ] Commit: `feat: record runtime memory telemetry`.

---

### Task 4: Make production depth-only and expose CLI knobs

**Files:**
- Modify: `src/chessgrandmaster/production_pipeline.py`
- Modify: `src/chessgrandmaster/analyzer.py`
- Modify: `src/chessgrandmaster/parallel_runner.py`
- Modify: `src/chessgrandmaster/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_run_recovery.py`

**Interfaces:**
- `run_pipeline(... workers=2, threads=1, hash_mb=256, multipv=1, depth=18, time_sec=0.0, snapshot_depths=(12,14,16,18,19))`.
- CLI flags: `--workers`, `--hash-mb`, `--depth`.
- `PIPELINE_VERSION = 2`.

- [ ] Extend CLI RED tests so defaults forward workers=2/hash=256/depth=18 and overrides forward 4/512/19. Add `positive_int()` rejection tests for zero/negative values.
- [ ] Run `pytest -q tests/test_cli.py`; expect FAIL.
- [ ] Add `--hash-mb` and `--depth`; call `run_pipeline(... workers=args.workers, hash_mb=args.hash_mb, depth=args.depth)`.
- [ ] Change production defaults from 3.0 seconds to 0.0 seconds in worker/runner/analyzer/pipeline. Do not add a time CLI flag.
- [ ] Set `PIPELINE_VERSION=2`; include `snapshot_depths` in engine/run identity and `config_json`.
- [ ] Add run-recovery regression: a legacy D18/3s run must not be recovered by a D18/time-off/snapshot-policy run.
- [ ] Run `pytest -q tests/test_cli.py tests/test_run_recovery.py`; expect PASS.
- [ ] Commit: `feat: make production analysis depth only`.

---

### Task 5: Update Molab to 4 workers / 512 MB / D19

**Files:**
- Modify: `notebooks/molab_b1.py`
- Modify: `tests/test_molab_notebook.py`

- [ ] Add RED test asserting the notebook contains one invocation equivalent to:

```python
[
    str(_analyzer),
    "--workers", "4",
    "--hash-mb", "512",
    "--depth", "19",
    str(_input),
]
```

and UI text includes `Hash/worker: 512 MB`, `Depth: 19`, `Time limit: OFF`, snapshots `12, 14, 16, 18, 19`.
- [ ] Run `pytest -q tests/test_molab_notebook.py`; expect FAIL.
- [ ] Update notebook invocation and displayed policy. Keep file-browser PGN input and existing output paths unchanged.
- [ ] Run `pytest -q tests/test_molab_notebook.py` and `python -m py_compile notebooks/molab_b1.py`; expect PASS.
- [ ] Commit: `feat: run Molab at depth 19 with 512mb hash`.

---

### Task 6: Audit snapshot completeness before using research data

**Files:**
- Modify: `src/chessgrandmaster/production_pipeline.py`
- Create: `tests/test_snapshot_audit.py`

**Interfaces:**
- `snapshot_audit(db_path, run_id, expected_depths) -> dict`
- Audit checks primary checkpoint completeness, post-move completeness when second search exists, and D(final) primary snapshot consistency with final primary rank-0 response.

- [ ] Write RED fixture tests: one complete D12/14/16/18/19 primary set gives zero missing; one post-move set missing D16 reports exactly one missing.
- [ ] Run `pytest -q tests/test_snapshot_audit.py`; expect FAIL.
- [ ] Implement audit using SQLite queries. For final consistency compare D(final) primary snapshot vs final `engine_responses` primary rank 0 on CP, mate, and first PV move.
- [ ] Print `DEPTH SNAPSHOT AUDIT`; raise `RuntimeError` if primary missing, post-move missing, or final mismatches are non-zero.
- [ ] Run `pytest -q tests/test_snapshot_audit.py tests/golden/test_tre_2026_game_1.py`; expect PASS.
- [ ] Commit: `test: audit depth snapshot completeness`.

---

### Task 7: Full verification before Molab 50-game run

**Files:** verify only.

- [ ] Run `pytest -q`; require zero failures.
- [ ] Run `pytest -q tests/golden/test_tre_2026_game_1.py`; require PASS.
- [ ] Run:

```bash
python -m py_compile \
  src/chessgrandmaster/engine_worker.py \
  src/chessgrandmaster/parallel_runner.py \
  src/chessgrandmaster/analyzer.py \
  src/chessgrandmaster/resource_telemetry.py \
  src/chessgrandmaster/production_pipeline.py \
  src/chessgrandmaster/cli.py \
  notebooks/molab_b1.py
```

- [ ] Inspect `git diff --stat b732d221..HEAD` and confirm no benchmark/TWIC scope creep.
- [ ] Do not create an empty verification commit. Any regression fix requires a failing test first and a focused commit.

## Molab validation acceptance criteria

The 50-game trial is valid only if the final database/output shows:

```text
workers=4
threads=1
hash_mb=512
depth_limit=19
time_limit_ms=NULL
failed=0
pending=0
snapshot depths=12,14,16,18,19
primary snapshot missing=0
post_move snapshot missing=0
final snapshot mismatches=0
runtime_resource_samples > 0
max Stockfish process count = 4
```

Runtime may be materially longer than the prior D18/3s run; that is an observation, not a failure. After the trial, compute line-stability metrics from the raw snapshots before deciding whether to keep D19 or return the production filter to D18.