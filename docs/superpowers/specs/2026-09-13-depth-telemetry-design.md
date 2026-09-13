# Depth-only search and engine telemetry design

Date: 2026-09-13
Branch: `chatgpt-work`

## Goal

Extend the B1 production analyzer so the research run is depth-driven rather than time-limited, while preserving enough intermediate engine information to study how stable a candidate line is as search depth increases.

The first live validation target is Molab with:

- 4 workers
- 1 Stockfish thread per worker
- 512 MB Hash per worker
- final depth 19
- no search time limit
- snapshot depths 12, 14, 16, 18, 19

Depth 19 is an experimental production setting for the next 50-game run. The architecture must also support a later return to final depth 18 without schema changes.

## Search policy

Production search becomes depth-only by default. `time_sec=0` means no time limit is sent to Stockfish. A depth limit remains mandatory for production analysis.

The normal B1/Lucas result still comes from the final search result at the requested final depth. For the Molab experiment this is depth 19. Classification, Lucas loss, NAG assignment, PGN export, and second-search behavior continue to use only that final result.

The current second-search rule is preserved: if the played move is not the primary best move, the post-move position is searched to the same final depth. Its score is converted back to the original mover's point of view and the played move is prepended to the PV.

## Intermediate depth capture

Each Stockfish search will be a single iterative search to the final depth, not five independent searches.

During that one search, B1 captures the latest engine info at checkpoints 12, 14, 16, 18, and 19. Checkpoints above the configured final depth are ignored. This avoids repeated work and, more importantly for research, observes the natural evolution of one search rather than restarting Stockfish at every depth.

Snapshots are captured for both:

- `primary`: the search from `fen_before` that determines the best line.
- `post_move`: the second search after the actually played move when Lucas semantics require it.

For `post_move`, every stored PV is normalized to the original position by prepending the played move. CP, mate and WDL are also normalized to the original mover's POV. That makes primary and post-move records directly comparable.

Stockfish will be configured with `UCI_ShowWDL=true`. B1 stores the engine-provided WDL triplet; it does not estimate WDL from centipawns.

## SQLite schema

Add a new auxiliary table. Existing `engine_responses` remains the source of the final response set used by classification and export.

```sql
CREATE TABLE IF NOT EXISTS engine_depth_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL,
    source_search TEXT NOT NULL,
    checkpoint_depth INTEGER NOT NULL,
    reported_depth INTEGER NOT NULL,
    uci TEXT,
    cp INTEGER,
    mate INTEGER,
    wdl_wins INTEGER,
    wdl_draws INTEGER,
    wdl_losses INTEGER,
    seldepth INTEGER,
    nodes INTEGER,
    nps INTEGER,
    time_ms INTEGER,
    pv_uci TEXT,
    UNIQUE(analysis_id, source_search, checkpoint_depth),
    FOREIGN KEY(analysis_id) REFERENCES move_analysis(id)
);
```

`checkpoint_depth` is the requested research checkpoint. `reported_depth` records the actual engine depth observed when the snapshot was taken. Normally they are equal, but keeping both prevents silent assumptions if an engine emits an unusual info sequence.

No stability score is baked into the schema yet. The raw data is intentionally preserved first. After the 50-game run we can define research metrics such as:

- first-move stability across checkpoints,
- common PV-prefix length across checkpoints,
- CP range / sign flips,
- WDL drift,
- mate appearance/disappearance,
- divergence between primary and post-move lines.

This keeps the first experiment auditable and avoids locking in an arbitrary complexity formula too early.

## Runtime memory telemetry

Because Molab/gVisor can expose misleading host-style CPU/RAM values, B1 must log observed runtime memory rather than trusting one static capacity probe.

Add a run-level table:

```sql
CREATE TABLE IF NOT EXISTS runtime_resource_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sampled_at TEXT NOT NULL,
    completed_positions INTEGER,
    total_positions INTEGER,
    configured_workers INTEGER,
    configured_hash_mb_per_worker INTEGER,
    configured_hash_total_bytes INTEGER,
    cgroup_memory_current_bytes INTEGER,
    cgroup_memory_max_bytes INTEGER,
    mem_total_bytes INTEGER,
    mem_available_bytes INTEGER,
    cgm_process_tree_rss_bytes INTEGER,
    stockfish_process_count INTEGER,
    stockfish_rss_bytes INTEGER,
    FOREIGN KEY(run_id) REFERENCES analysis_runs(id)
);
```

Sampling points:

1. immediately after the worker pool is started,
2. after every normal analysis checkpoint/batch,
3. immediately before the worker pool is closed.

Collection uses Linux `/proc`, cgroup files where available, and the actual process tree. Missing or untrustworthy platform fields are stored as `NULL` or treated as observations only; they are never used to auto-increase Hash.

At the end of a run, the pipeline prints a compact memory summary with:

- configured total Hash,
- peak Stockfish RSS,
- peak CGM process-tree RSS,
- peak observed cgroup memory usage,
- minimum observed `MemAvailable`,
- reported cgroup limit if one exists,
- number of Stockfish processes seen.

This gives us real evidence for deciding whether 512 MB/worker is comfortable and whether a future 1 GB/worker test is justified. B1 will not auto-tune Hash in this phase.

## Run metadata and timestamps

The new search policy changes run identity. Increment `PIPELINE_VERSION` so a depth-only run never recovers the previous time-limited run.

`analysis_runs` records:

- final depth,
- no time limit (`time_limit_ms` stored as `NULL`),
- workers,
- threads,
- Hash per worker,
- snapshot depth list in `config_json`,
- `created_at` timestamp.

Move jobs also record real `started_at` and `finished_at` values supplied by the worker result. This fixes the current telemetry gap and allows later progress/ETA calculations from SQLite without requiring a concurrently executing marimo cell.

## CLI and Molab policy

Extend `cgm-analyze` with production knobs needed by the notebook:

```text
--workers N
--hash-mb N
--depth N
```

The normal production defaults remain conservative and portable:

- workers: 2
- threads: 1
- Hash: 256 MB/worker
- depth: 18
- time limit: off

Molab explicitly runs the next experiment as:

```text
cgm-analyze --workers 4 --hash-mb 512 --depth 19 <input.pgn>
```

The notebook UI/result block states that policy and displays the end-of-run resource summary. A later fallback to depth 18 only changes the Molab invocation; it does not require a schema migration.

## Storage and recovery behavior

Depth snapshots and resource samples are written in the same batch transaction/checkpoint rhythm as analysis results so the database remains recoverable after interruption.

When an analysis row is retried, its old final `engine_responses` and old `engine_depth_snapshots` are deleted before replacement, preventing duplicate telemetry for the same `(run_id, move_id)`.

Resource samples are append-only for the run. They describe the run's environment, not an individual move.

## Testing strategy

Implementation follows TDD.

Required tests include:

1. depth-only engine limits contain depth but no time value,
2. one iterative primary search emits snapshots for requested depths,
3. final result still matches the final-depth engine info,
4. post-move snapshots prepend the played move and preserve original-mover POV,
5. WDL is stored from engine info at each checkpoint,
6. schema migration creates both telemetry tables safely on existing databases,
7. retry replaces old depth snapshots rather than duplicating them,
8. run identity distinguishes the new depth-only/snapshot policy from legacy 3-second runs,
9. CLI forwards `--workers`, `--hash-mb`, and `--depth`,
10. Molab notebook invokes 4 workers / 512 MB / depth 19,
11. resource sampling tolerates unavailable cgroup/proc fields,
12. end-of-run resource summary is computed from stored samples.

The existing Lucas R6 golden semantics and exporter tests must remain green.

## Explicit non-goals for this phase

- no automatic depth selection,
- no automatic Hash increase,
- no new benchmark suite,
- no tactic-complexity score yet,
- no change to Lucas classification thresholds,
- no change to PGN export semantics,
- no TWIC splitting work in this change.

The immediate research output is a clean 50-game Molab database containing final D19 analysis, intermediate D12/D14/D16/D18/D19 snapshots for both search types, and run-level memory telemetry.
