# Processor Finalization Performance Repair

Date: 2026-09-20
Branch: `chatgpt-work`
Baseline remote HEAD: `b2c31d2d9589bbee8f2e593626639af885abfccd`

## Why this task exists

Production `cgm-process` proved that raw parsing is not the current bottleneck.
At the latest read-only snapshot:

- `CANONICALIZED = 1564`
- `DOWNLOADED = 172`
- `DISCOVERED = 2`
- `game_occurrences = 116050`
- `ProcessingTimeout = 0`

Workers were mostly idle while the parent/finalizer consumed CPU.
The hot Registry queries filtered `game_occurrences` by
`canonical_game_id`, but the only existing index was the unique
`(source_file_id, source_game_index)`.

`EXPLAIN QUERY PLAN` therefore showed full scans for
`get_occurrences()` and `list_valid_occurrence_candidates()`.
## Reproduced benchmark on a TEMP copy of production registry

No production bytes were modified. A temporary copy was created and deleted.

Representative latest revision: 170 canonical games.

Before index:

- 170 x `get_occurrences`: ~13499 ms
- 170 x global candidate lookup: ~12003 ms
- query plan: `SCAN game_occurrences` / `SCAN go`

After adding:

```sql
CREATE INDEX idx_game_occurrences_canonical_valid_tournament
ON game_occurrences(canonical_game_id, is_valid, tournament_id);
```

Results:

- 170 x `get_occurrences`: ~12.7 ms
- 170 x global candidate lookup: ~9.1 ms
- query plan changed to indexed SEARCH

This is roughly three orders of magnitude faster for these hot lookups.
The temp benchmark database was removed afterwards.

## Staged local repair by ChatGPT

The local working tree currently adds the composite index to schema v1
with `CREATE INDEX IF NOT EXISTS`, so opening an existing v1 Registry
repairs the missing index without a destructive migration or version bump.
Focused tests were added to prove:

1. new registries contain the index;
2. reopening an existing v1 registry recreates a missing index;
3. the canonical lookup query plan no longer reports a full table scan.

Current checks already run:

- `tests/b2/test_registry.py`: 15 passed
- `tests/b2`: 136 passed
- full-suite run was started; re-check before integration.

## Hard boundaries for all three subagents

- Do not modify production `.cgm/corpus-2026` during coding/tests.
- Do not run Stockfish or change B1.
- Do not change canonical fingerprint semantics.
- Do not change provenance/occurrence precedence.
- Do not change raw immutable objects.
- Do not implement B2b here.
- No title/name heuristics.
- No schema rewrite or destructive migration.
- Use temporary registries/object stores only.
- Prefer evidence over speculative refactoring.
- Each agent works in its own worktree/branch from the integration SHA.
- Commit only its scoped work; no merge/main/force-push.

## Subagent Luna A — Registry index correctness

Own: `src/chessgrandmaster/b2/registry.py`,
`tests/b2/test_registry.py`.

Review the staged composite index repair. Confirm it is the minimal useful
index for all three hot shapes:
- `WHERE canonical_game_id = ?`
- `WHERE canonical_game_id = ? AND is_valid = 1`
- same plus `tournament_id = ?`

Add/adjust query-plan tests for global and tournament-local candidate
lookups. Preserve candidate ordering exactly. Verify opening an old v1
database creates the index idempotently.

Do not add extra indexes without an observed query need.

Suggested commit:
`perf: index canonical occurrence lookups`

## Subagent Luna B — Finalizer hotspot audit

Own primarily: `src/chessgrandmaster/b2/canonicalize.py` and a new focused
test/benchmark file; avoid touching Registry unless absolutely necessary.

Measure the post-index finalizer path with a TEMP registry large enough to
expose scaling. Check separately:

- metadata conflict refresh
- global display occurrence selection
- tournament-local occurrence selection
- projection/export
- revision finalization

If the index removes the practical stall, DO NOT refactor further.
If a remaining N+1/query hotspot is still material, make the smallest
semantics-preserving batching change and prove byte/SHA compatibility with
the existing canonicalizer.

Suggested commit only if code changes are justified:
`perf: reduce canonical finalizer query overhead`
## Subagent Luna C — Resume and production-safety acceptance

Own: processor tests/docs/runbook; avoid canonical semantics changes.

Using TEMP data only, prove:

- an interrupted DOWNLOADED tournament converges idempotently;
- CANONICALIZED tournaments are skipped;
- invalid occurrences remain reviewable;
- timeout worker replacement still works;
- no duplicate occurrences/revisions are created;
- timing/report output remains correct.

Add a short production resume runbook with exact commands and the
read-only checks to perform while the real 172-tournament backlog runs.
Do not start the real production processor unless the user explicitly
approves after integration.

Suggested commit:
`test: harden processor resume acceptance`

## Integration gate

Lead Luna should integrate A first, then B only if benchmark evidence shows
additional material gain, then C. Run:

```bat
set PYTHONPATH=src
.venv\Scripts\python.exe -m pytest tests\b2 -q
.venv\Scripts\python.exe -m pytest tests\discovery -q
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
```

Only after all green: resume `cgm-process` on production and monitor
DOWNLOADED/CANONICALIZED plus per-stage timing. The goal is to finish B2a
quickly and move to B2b/B1 analysis, including the new Oracle Cloud option.
