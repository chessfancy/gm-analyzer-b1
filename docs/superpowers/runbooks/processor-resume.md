# B2 Processor Resume Runbook

## Safety gate

This runbook documents how to inspect and resume a real B2 `DOWNLOADED` backlog. **Do not run the production `cgm-process` command without separate, explicit user approval.** The Luna C task does not start it. Do not run `cgm-acquire`, delete registry rows, edit statuses, or start B1/Stockfish as part of a resume.

Run the commands below from the repository root. They use the Windows project virtual environment and the production paths `.cgm/registry.sqlite` and `.cgm/b2`.

## Read-only preflight

Set the import path if invoking the module directly:

```bat
set PYTHONPATH=src
```

Inspect the global registry. `cgm-registry status` opens the database read-only:

```bat
.venv\Scripts\cgm-registry.exe status --registry .cgm\registry.sqlite
```

Confirm the JSON `tournament_status_counts.DOWNLOADED` value is the backlog to resume, and record the `CANONICALIZED`, `SHARDED`, and `READY` counts. For one tournament ID from the global listing, inspect provenance and existing revisions without writing:

```bat
.venv\Scripts\cgm-registry.exe status 1234 --registry .cgm\registry.sqlite
```

Replace `1234` with the literal tournament ID being reviewed. A per-tournament check must show its current status and source/revision metadata; do not manually change a `DOWNLOADED` status.

This read-only SQLite check detects duplicate source occurrence keys and duplicate content-addressed revisions. It uses SQLite `mode=ro` and must print `0` for both duplicate counts:

```bat
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('file:.cgm/registry.sqlite?mode=ro', uri=True); print(c.execute('SELECT status, COUNT(*) FROM tournaments GROUP BY status ORDER BY status').fetchall()); print('duplicate_occurrence_keys=', c.execute('SELECT COUNT(*) FROM (SELECT source_file_id, source_game_index FROM game_occurrences GROUP BY source_file_id, source_game_index HAVING COUNT(*) > 1)').fetchone()[0]); print('duplicate_revisions=', c.execute('SELECT COUNT(*) FROM (SELECT tournament_id, canonical_sha256, canonicalization_policy FROM tournament_revisions GROUP BY tournament_id, canonical_sha256, canonicalization_policy HAVING COUNT(*) > 1)').fetchone()[0]); c.close()"
```

List invalid occurrences for review without changing them:

```bat
.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('file:.cgm/registry.sqlite?mode=ro', uri=True); print(c.execute('SELECT tournament_id, source_file_id, source_game_index, is_valid, canonical_game_id, parse_error FROM game_occurrences WHERE is_valid = 0 ORDER BY tournament_id, source_file_id, source_game_index').fetchall()); c.close()"
```

## Approved resume command

Run this command only after the separate user approval above. It processes the `DOWNLOADED` queue once, skips `CANONICALIZED` tournaments, writes one JSON report, and performs no network fetch:

```bat
if not exist .cgm\reports mkdir .cgm\reports
.venv\Scripts\cgm-process.exe --registry .cgm\registry.sqlite --root .cgm\b2 --workers auto --game-timeout-sec 2 --report .cgm\reports\process-latest.json
```

If the process is interrupted, do not delete rows or objects and do not force a status. Repeat the same preflight checks, then repeat the exact resume command only with approval. A successful rerun should converge the still-`DOWNLOADED` tournament without duplicate occurrence rows or revisions.

## Read-only monitoring and postflight

During and after the run, repeat the global status check:

```bat
.venv\Scripts\cgm-registry.exe status --registry .cgm\registry.sqlite
```

Read the saved report without modifying the registry:

```bat
type .cgm\reports\process-latest.json
```

Check that `ok` is `true`, `failed_sources` is `0`, and that `selected_tournaments`, `processed_tournaments`, `canonicalized`, `review_required_sources`, `invalid_games`, `timed_out_games`, `duplicate_occurrences`, `worker_replacements`, and `timings` are internally consistent. `duplicate_occurrences` is an audit count of repeated valid games in source input; it is not permission to create duplicate rows.

For every `review_required` result or persistent invalid occurrence, retain the source/tournament ID for review. Invalid occurrences should remain visible with `is_valid = 0`, a non-empty `parse_error`, and no `canonical_game_id`; do not mark those tournaments `CANONICALIZED` manually. Re-run the read-only SQLite duplicate check after the processor exits.
