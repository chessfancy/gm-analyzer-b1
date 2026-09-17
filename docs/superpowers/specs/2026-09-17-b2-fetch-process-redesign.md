# B2 Fetch/Process Redesign

Date: 2026-09-17
Branch: `chatgpt-work`
Status: Phase A and Phase B implemented; B2b remains deferred

## Goal

Separate network acquisition from CPU-heavy PGN processing so a large Chess-Results corpus can finish downloading without making each subsequent download wait for full `python-chess` validation and canonicalization.

The redesign preserves the existing B2 portable unit:

```text
registry.sqlite + objects/
```

The registry and immutable object store remain the handoff boundary between the network stage and the local processing stage. B1 analysis semantics and B2b packaging are outside this change.

## Non-goals and frozen boundaries

- Do not change B1 chess semantics, Lucas classification, Stockfish search policy, depth policy, snapshot policy, scheduler, game affinity, platform resource profiles, Hash settings, PGN export behavior, raw UCI telemetry, `JobSpec`, or `PIPELINE_VERSION`.
- Do not implement B2b, serverless workers, or an external queue service. `cgm-process` is implemented only in the separate Phase B commit.
- Do not run the production full-year corpus as validation for this change.
- Do not modify the user's production `.cgm/corpus-2026` registry, objects, or workspace.
- Do not replace or delete immutable raw PGN objects.
- Do not infer Chess-Results family relationships from neighboring IDs, names, or tournament numbers.

## Architecture

### Phase A — `cgm-fetch` (implemented in this task)

```text
Chess-Results discovery
    -> corpus completeness gate
    -> optional priority enrichment (warning-only)
    -> PGN availability probe
    -> raw PGN download
    -> provider response guard (non-empty, not HTML, PGN-like)
    -> SHA256 and content-addressed immutable object
    -> source/source_tournament/source_file/download_attempt provenance
    -> DOWNLOADED when the current status permits advancement
    -> next candidate immediately
```

The fetch stage must not parse games. In particular it must not invoke `_validation_summary`, `identify_game`, `canonicalize_source_file`, or any equivalent `python-chess` full-PGN path. A downloaded tournament is ready for the later processor, not canonicalized.

For a fetch-stage-completed candidate (`DOWNLOADED`, `VALIDATED`, `CANONICALIZED`, `SHARDED`, or `READY`) with `--refresh-recent-days 0`, fetch returns `skipped_existing` and does not probe or download. The existing full `acquire_candidates()` path intentionally keeps its older retry semantics for `DOWNLOADED`/`VALIDATED`; this fetch-only rule is scoped to the new raw stage. Refresh support may re-download only an eligible recent `CANONICALIZED` candidate; it never regresses status and never replaces an immutable source file.

One candidate's probe, download, or provenance failure is isolated into a structured result. The batch continues with later candidates.

A successful provider download that yields zero bytes is a typed `NoUsablePgnError` with stable reason `empty_pgn`. It records the failed download attempt for audit, creates neither a raw object nor a `source_file`, and is reported as `skipped_no_pgn` rather than `failed`. Timeouts, connection/HTTP errors, storage errors, HTML/error bodies, and other operational failures remain `failed` and retryable; no title or external-ID blacklist is persisted.

### Phase B — `cgm-process` (implemented in the separate processor commit)

```text
DOWNLOADED source files
    -> one-shot local processor
    -> bounded worker pool / replaceable worker processes
    -> one parse of each source game
    -> per-game validation + identity + canonical projection
    -> serialized/controlled SQLite writer
    -> canonical revision and membership
    -> CANONICALIZED
    -> exit when the selected queue is empty
```

`cgm-process` is a one-shot command, not a daemon. It consumes raw source files already recorded in the registry and exits when the selected queue is empty. Cross-machine copying of `registry.sqlite + objects/` is supported by the layout but is secondary to local processing.

## Phase A contract

### Candidate flow

For each unique `(provider, external_id)` candidate:

1. Look up the exact source identity in the registry.
2. If its status is completed for the fetch stage (`DOWNLOADED`, `VALIDATED`, `CANONICALIZED`, `SHARDED`, or `READY`) and refresh is disabled, return `skipped_existing`.
3. Otherwise run the provider PGN availability probe.
4. If unavailable, return `skipped_no_pgn` without creating a source file or raw object.
5. Resolve/describe the source and download into a temporary file.
6. Reject empty data and provider HTML/error responses using the existing adapter protections.
7. Compute SHA256, write an immutable content-addressed raw object, and verify size/hash.
8. Record the source file and successful download attempt.
9. Advance status to `DOWNLOADED` only when that is not a regression from a later state.
10. Return the result and continue immediately.

No canonical game, occurrence, revision, or canonical PGN is created in Phase A.

### Completeness and priority

Broad corpus fetch is fail-closed on source discovery:

```text
complete = no source-window errors AND no saturated source windows
```

When `complete == false`, `cgm-fetch` must not construct B2 or begin any probe/download. It returns a structured `CorpusIncompleteError` payload and may write the report.

Priority enrichment is independent:

- `priority_complete == false` is a warning.
- `priority_errors` are preserved in stdout/report.
- Priority failure does not block fetching a complete source corpus.

### Provenance and immutability

The existing B2 schema remains the source of truth. Fetch writes only the acquisition-side tables needed for provenance:

- `sources`
- `tournaments`
- `source_tournaments`
- `source_files`
- `download_attempts`

A raw object key remains content-addressed by tournament/provider/SHA and is never overwritten. A repeat of the same SHA reuses the existing `source_file` row while recording a new download attempt. A changed raw response receives a new object and source-file row. Phase A does not write:

- `canonical_games`
- `game_occurrences`
- `game_metadata_conflicts`
- `tournament_revisions`
- `tournament_games`

### Report contract

`cgm-fetch` emits one deterministic JSON object on stdout and can write the same audit-oriented corpus report with sorted keys. At minimum it includes:

```json
{
  "candidate_count": 0,
  "complete": true,
  "priority_complete": true,
  "priority_errors": [],
  "priority_counts": {"book_high": 0, "book_medium": 0, "general": 0},
  "fetched": 0,
  "skipped_existing": 0,
  "skipped_no_pgn": 0,
  "failed": 0,
  "results": []
}
```

Each result identifies `provider`, `external_id`, `source_url`, `action`, and tournament status. A fetched result also exposes `tournament_id`, `source_file_id`, `raw_sha256`, object key, and download attempt provenance. A failure exposes `error_type` and a stable, whitespace-normalized `error_message`. Raw PGN bytes never appear in JSON.

## Phase B processor contract

### One-shot queue and work selection

- Select source files with status `DOWNLOADED` (and explicit retry/review policy later), in deterministic registry order.
- Do not start a background daemon or leave a queue service running.
- Every source file remains available for retry because the raw object is immutable.
- A processor run must be resumable: completed source games/occurrences and revision work are idempotently recognized rather than duplicated.

### Per-game failure isolation

A source file may contain valid and invalid games. Parse/processing failure skips only that game:

```text
canonical_game_id = NULL
is_valid = false
parse_error = stable error string
raw_pgn_object_key = retained
```

The source file and game occurrence remain auditable. Do not abort the whole tournament because one game is malformed. Do not attempt automatic SAN repair. The existing raw PGN object is the evidence.

The stable timeout form is:

```text
ProcessingTimeout: exceeded 2.000s
```

The default game timeout is `2.0` seconds, measured after a worker actually starts processing that game, not from queue submission time. A timed-out worker must be killable and replaceable; merely calling `Future.result(timeout=...)` while leaving the stuck process alive is not sufficient.

### Parse-once and controlled writes

The processor must parse each normal game once. It must not retain a validate-then-parse-again pattern. CPU work may run in parallel, but SQLite writes are performed through a controlled serialized writer (or an equivalently bounded ownership protocol) to avoid writer contention and make commits auditable.

### Timing instrumentation

Record separate timings for:

- source-game segmentation/read;
- `python-chess` parse;
- identity/replay/canonical projection;
- SQLite commit/update;
- source-file finalization/revision creation.

The observed local reference for one real canonical 125-ply game was median 4.03 ms, p95 5.10 ms, p99 5.85 ms, max 12.46 ms over 1,000 repetitions. The 2.0-second timeout is deliberately conservative. Instrumentation is required because a prior multi-hour stall has not been proven to be inside `python-chess`.

### Completion and status

After all selected source games are accounted for, the writer creates/updates the canonical revision and membership, then advances the tournament to `CANONICALIZED` without regressing later states. The one-shot process exits with a structured summary after the queue is empty. B2b may later advance the status to `SHARDED` and `READY`.

## Compatibility

- Existing `cgm-discover` remains the discovery CLI and its `--acquire` path remains backward-compatible full B2a acquisition.
- Existing `AcquisitionService.acquire()` remains available and continues to validate/canonicalize.
- `cgm-fetch` is an explicit new boundary; it does not silently change `cgm-discover` or `cgm-acquire` semantics.
- Provider-specific form/probe/download protections stay in the Chess-Results adapter. No second provider discovery implementation is introduced.

## Acceptance for this slice

Automated tests must prove fetch-only registration/status, absence of all canonical tables and canonicalization calls, immutable raw provenance, idempotent refresh-disabled rerun, unavailable/failed candidate isolation, incomplete-corpus fail-closed behavior, priority warning behavior, deterministic JSON/report shape, and preservation of the existing discovery/B2 suite.

One bounded live smoke test may use `tnr1450909` with a temporary registry/root only. It must end at `DOWNLOADED`, have raw provenance, have zero canonical rows and no canonical PGN. The production `.cgm/corpus-2026` path is never used.
