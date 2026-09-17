# B2 Fetch/Process Redesign Implementation Plan

> Scope: Phase A `cgm-fetch` is complete; this document records that implementation and the approved Phase B `cgm-process` follow-up. Production processing remains out of scope.

**Goal:** Add an explicit fetch-only corpus command that discovers and probes Chess-Results candidates, preserves immutable raw PGNs with provenance, and advances eligible tournaments to `DOWNLOADED` without invoking full-PGN parsing or canonicalization. Then implement the approved local Phase B processor over that registry/object-store boundary.

**Architecture:** Extract the existing B2a download/register portion behind `AcquisitionService.fetch()`. Keep `AcquisitionService.acquire()` as the backward-compatible full validation/canonicalization API. Add a fetch operation to the existing discovery-to-acquisition bridge instead of creating another Chess-Results discovery implementation. Add `cgm-fetch` as a separate JSON CLI that reuses `ChessResultsDiscovery`, the completeness gate, priority enrichment, the existing adapter, `Registry`, and `LocalObjectStore`. Add `cgm-process` as a local-only one-shot command with a persistent replaceable process pool and a parent-owned canonical writer.

**Verification baseline:** the Phase B follow-up starts at exact HEAD `c412fca90793274fb6188782a96d7dbf886c26ff`, clean worktree, and the Phase A baseline of 313 passed / 1 skipped. Production `.cgm/corpus-2026` is out of scope and must not be opened, tested, or mutated.

## Global constraints

- Work only on `chatgpt-work`; no PR, merge to `main`, force-push, or history rewrite.
- Keep the empty-PGN fix and processor implementation as two focused commits; push both only after all scoped verification, then stop.
- Do not run the production full-year corpus or use `.cgm/corpus-2026` in tests/live smoke.
- Do not modify B1, B2b, `JobSpec`, `PIPELINE_VERSION`, scheduler, Stockfish, or analysis output.
- Do not weaken existing tests.
- Normal tests stay fixture-only and do not require live network access.
- Preserve source/provider identifiers and raw object content exactly.

## File targets

```text
docs/superpowers/specs/2026-09-17-b2-fetch-process-redesign.md
docs/superpowers/plans/2026-09-17-b2-fetch-process-redesign.md
src/chessgrandmaster/b2/acquisition.py          # extract fetch-only path
src/chessgrandmaster/b2/canonicalize.py         # reusable processed-game finalizer
src/chessgrandmaster/b2/processor.py            # local worker pool and corpus processor
src/chessgrandmaster/b2/cli_process.py          # new cgm-process CLI
src/chessgrandmaster/b2/registry.py              # deterministic source-file selection support
src/chessgrandmaster/discovery/acquire.py       # shared candidate bridge operation
src/chessgrandmaster/b2/cli_fetch.py            # new cgm-fetch corpus CLI
pyproject.toml                                  # cgm-fetch entry point
tests/b2/test_acquisition.py                   # fetch service invariants
 tests/discovery/test_discovery_acquisition.py  # fetch bridge isolation/idempotence
 tests/b2/test_fetch_cli.py                     # CLI/report/completeness contract
tests/b2/test_processor.py                      # Phase B processing/timeout contract
```

The leading spaces in the test paths above are only visual grouping; use the repository paths without spaces.

---

## Task 1 — Record the approved design and plan

- [x] Create the spec with the two-phase architecture, boundaries, raw provenance, processor timeout/isolation/parse-once/writer/timing decisions, and Phase A acceptance contract.
- [x] Create this implementation plan before source changes.
- [x] Read both files back and check that Phase B is documented before its separate implementation commit.

Verification: `git diff --check` after the first implementation batch.

## Task 2 — RED: prove the current service is not fetch-only

### Step 2.1: Add a failing service test

Add a focused test using the existing fixture service:

- call the new intended `AcquisitionService.fetch("tnr1450909")` API;
- monkeypatch `_validation_summary`, `canonicalize_source_file`, and `identify_game` in `chessgrandmaster.b2.acquisition` with sentinels that raise;
- assert the fetched result has a raw SHA/source file and `DOWNLOADED` status;
- assert canonical tables are empty.

At RED stage, the missing method/import should fail for the intended reason. Do not change production code to make a test pass before recording the failure.

Run: `pytest tests/b2/test_acquisition.py -k fetch -q`.

### Step 2.2: Add failing bridge/CLI contract tests

Add fixture tests for:

- `fetch_candidates()` downloads an available candidate and returns action `fetched` with `source_file_id` and no revision;
- a second run with refresh `0` returns `skipped_existing` without another probe/download;
- unavailable PGN leaves all raw/provenance counts at zero;
- a failing first candidate is reported and the later candidate is still fetched;
- incomplete corpus returns `CorpusIncompleteError` before B2 construction;
- priority enrichment error keeps `complete=true` and still fetches;
- CLI parser accepts `chess-results corpus --year --max-lines --refresh-recent-days --registry --root --report`;
- stdout and report expose the minimum deterministic fetch fields.

Run the focused tests and record RED before implementation.

---

## Task 3 — GREEN: extract a reusable download/register operation

### Step 3.1: Introduce fetch result data

In `b2/acquisition.py`, add a small immutable `FetchResult` containing:

- source ref/provider identity;
- source/tournament/source-tournament IDs;
- source-file ID;
- raw object key, SHA256, and byte size;
- download attempt ID;
- actual tournament status.

Do not expose raw PGN bytes or a temporary path in the result.

### Step 3.2: Factor download/register logic

Extract the existing resolve → describe → registry upsert → temporary adapter download → empty/content/hash/store verification → source file → successful/failed attempt → status advancement sequence into a private operation used by both methods.

Implement `AcquisitionService.fetch(query)` by running that operation and returning immediately after `DOWNLOADED` advancement. It must not call `_validation_summary`, `identify_game`, or `canonicalize_source_file`.

Keep `AcquisitionService.acquire(query)` behavior unchanged by supplying a callback to the extracted operation. The callback performs the existing validation, status advancement, canonicalization, and full `AcquisitionResult` construction while the temporary downloaded file is still available.

Preserve:

- existing adapter protections against HTML/provider errors;
- immutable object keys and SHA/size verification;
- download-attempt error recording;
- no status regression for later states;
- existing full-acquisition return fields and tests.

Run:

```bash
pytest tests/b2/test_acquisition.py -k fetch -q
pytest tests/b2/test_acquisition.py -q
```

### Step 3.3: Add source-file provenance to bridge results

Extend `CandidateAcquisition` with an optional `source_file_id` and include it in `to_dict()`. Populate it from both full and fetch service results without breaking old constructors. Keep `revision_id` null for fetch-only outcomes.

---

## Task 4 — GREEN: add fetch mode to the existing candidate bridge

Refactor only the candidate-operation selection in `discovery/acquire.py`; do not duplicate provider discovery or probe code.

- Retain `acquire_candidates()` exactly for backward-compatible full B2a acquisition.
- Add `fetch_candidates()` using the same exact-identity dedupe, completed-status handling, probe validation, per-candidate failure isolation, and recent-date checks.
- Call `AcquisitionService.fetch()` in fetch mode.
- Use action `fetched` for first-time/raw acquisition; preserve `skipped_existing`, `skipped_no_pgn`, `failed`, and the optional recent refresh actions.
- Count successful raw fetches through the existing batch summary in a way the CLI can expose as `fetched`.
- Return `source_file_id`, raw SHA/object provenance, actual status, and no revision for fetched outcomes.
- With refresh `0`, completed statuses must not probe or redownload. `SHARDED` and `READY` must never regress.

Run the focused bridge tests and then the full existing discovery-acquisition test module.

---

## Task 5 — GREEN: implement `cgm-fetch`

### Step 5.1: Add parser and entry point

Create `b2/cli_fetch.py` with a JSON-only corpus interface:

```text
cgm-fetch chess-results corpus \
  --year 2026 \
  --max-lines 2000 \
  --refresh-recent-days 0 \
  --registry PATH \
  --root PATH \
  --report PATH
```

Support ordinary argparse shell continuation; no Windows-specific parsing is needed. Add `cgm-fetch = "chessgrandmaster.b2.cli_fetch:main"` to `pyproject.toml`. Keep `cgm-discover`, `cgm-acquire`, and `cgm-registry` entry points unchanged.

Use the existing `ChessResultsDiscovery.discover_corpus()` and `enrich_corpus_priority()`. Do not apply `--limit` as a discovery scan stop; if a limit is offered, apply it only after complete scanning/enrichment.

### Step 5.2: Preserve fail-closed/warning-only behavior

- If source discovery is incomplete, write an optional report and return a structured failure without constructing Registry/ObjectStore or invoking the bridge.
- If priority enrichment has errors but source discovery is complete, retain `priority_complete=false` and `priority_errors`, then fetch all discovered candidates.
- Keep all errors as stable JSON fields; no traceback/banner/credential output.

### Step 5.3: Emit deterministic fetch report

Expose at minimum:

- `candidate_count`, `complete`, `priority_complete`, `priority_errors`, `priority_counts`;
- `fetched`, `skipped_existing`, `skipped_no_pgn`, `failed`, `results`;
- provider/external ID and audit provenance for each result;
- `source_file_id` and `raw_sha256` when fetched;
- `error_type`/`error_message` when failed.

Do not include raw PGN. Use sorted-key JSON and stable result order from the discovery/bridge path. Write the report atomically enough for a normal local CLI file output and include the same acquisition summary.

Run:

```bash
pytest tests/b2/test_fetch_cli.py -q
```

---

## Task 6 — Regression verification

Run focused tests first, then the required suite without touching production paths:

```bash
pytest tests/b2/test_acquisition.py tests/discovery/test_discovery_acquisition.py tests/b2/test_fetch_cli.py -v
pytest tests/discovery -v
pytest tests/b2 -v
pytest -q
python -m compileall -q src tests
git diff --check
git show --check --oneline --no-renames HEAD
git status --short
```

Acceptance: existing baseline remains green; new tests add coverage for all eleven requested fetch-only behaviors. Verify no source file under B1 or B2b changed.

---

## Task 7 — One bounded live golden smoke test

Only after automated tests pass, run one live fetch-only smoke test for:

```text
tnr1450909
Giải Vô địch Cờ vua trẻ quốc gia năm 2026
Cờ tiêu chuẩn: Nam U07
```

Use a newly created temporary registry/root outside `.cgm/corpus-2026`, and call the bounded `AcquisitionService.fetch("tnr1450909")` path directly. Do not invoke the corpus-discovery CLI or run a full-year scan for live validation. Verify by reading back the temporary registry with the existing registry APIs:

- raw download/source file/download attempt exists;
- raw SHA/object exists and size/hash match;
- tournament status is `DOWNLOADED`;
- `canonical_games == 0`;
- `game_occurrences == 0`;
- `tournament_revisions == 0`;
- no canonical PGN exists.

Remove only the temporary smoke directory if convenient. Do not inspect or mutate production `.cgm/corpus-2026`.

---

## Task 8 — Commit A checkpoint

Commit the empty-PGN semantic fix separately after its focused verification:

```text
fix: classify empty PGN as unavailable
```

Do not push or start production processing until the Phase B work below is also verified.

---

## Task 9 — Implement and verify Phase B `cgm-process`

Implement the approved Phase B contract from the spec with these boundaries:

- select exact `DOWNLOADED` tournaments only and choose the latest source file by existing timestamp/id precedence;
- restore and verify immutable objects locally, then segment raw PGN lexically without a second source parse;
- send one picklable game job to a persistent `spawn` process pool;
- begin the per-game deadline only after a worker sends `started`;
- terminate/join/verify a timed-out worker, replace it, and record `ProcessingTimeout` as an invalid occurrence;
- let the parent be the only Registry writer and feed the reusable processed-game finalizer;
- preserve existing identity, duplicate, conflict, display/local-occurrence, projection, revision, and status semantics;
- emit stage timing aggregates and a JSON report without raw PGN text;
- keep zero-valid sources at `DOWNLOADED` with retained invalid evidence and continue the queue.

Run focused processor tests, then the B2/discovery/full regression suites, compileall, and diff checks. Use only temporary registry/object-store roots for acceptance; never run the production processor.

---

## Task 10 — Push and stop

Before push, verify exact scope and current branch/remote:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse origin/chatgpt-work
git diff --stat
git diff --check
git show --check --oneline --no-renames HEAD
```

Push both focused commits only to `origin/chatgpt-work`:

```text
fix: classify empty PGN as unavailable
feat: add parallel corpus processor
```

Verify the pushed SHA with `git rev-parse HEAD` and `git ls-remote origin refs/heads/chatgpt-work`, then stop. Do not start `cgm-process` against the production corpus, TWIC, B2b, or Stockfish.

Final report must state starting/final HEAD, both commit SHAs/messages, changed files, exact `cgm-process` interface, auto-worker formula, timeout/segmentation/parse-once/writer/idempotency strategies, empty-PGN and retry semantics, focused/full test results, compileall and whitespace results, bounded TEMP acceptance result, production-path untouched confirmation, B1 untouched confirmation, and any concrete blocker/design discrepancy.
