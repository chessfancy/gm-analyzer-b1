# Handoff: B2b + Multi-source + Distributed Analysis

Date: 2026-09-20
Branch: `chatgpt-work`
Baseline HEAD: `aa8d537c2a2253ffe19fa27eef3ddeb700ab5dae`

## Production snapshot

B2a Chess-Results is operational and currently drained.

- tournaments: 1,758
- CANONICALIZED: 1,757
- DISCOVERED: 1
- DOWNLOADED: 0
- source_files: 1,757
- canonical_games: 124,828
- game_occurrences: 126,506
- tournament_revisions: 1,757
- tournament_games: 125,340

The sole DISCOVERED item is `tnr1430071`, a test event whose provider
returns an empty PGN. It is correctly classified as
`skipped_no_pgn / empty_pgn` and is not a blocker.
The processor stall was fixed by indexing occurrence lookup on
`(canonical_game_id, is_valid, tournament_id)`. The remaining 172
tournaments then completed with 0 processing timeouts.

A later full Chess-Results refresh discovered 21 additional usable
events; all 21 were fetched and canonicalized.

Important production reports:

- `.cgm/corpus-2026/process-2026-after-index.json`
- `.cgm/corpus-2026/fetch-2026-postprocessor-retry.json`
- `.cgm/corpus-2026/process-2026-postfetch.json`

Do not delete, rewrite, or migrate the production corpus destructively.

## Multi-source rule

Every new provider must feed the same B2a model:

raw provider bytes -> immutable object -> provenance -> canonical chess
identity -> occurrence -> tournament revision -> B2b package.

Cross-source duplicates are expected. Preserve all provenance; dedupe by
the existing canonical chess fingerprint only.
## Source priorities

### Chess-Results
Status: production-ready. A monthly discovery timeout makes a run
`complete=false` by design; a later full production run completed with
no errors. Add only small bounded retry/backoff later if cron needs it.

### TWIC
Next bulk historical source. Start at issue 1660 and continue weekly.
Preserve raw archive/PGN provenance, filter to standard/classical OTB,
then canonicalize with the existing identity path. Do not invent a new
TWIC-specific fingerprint.

### Lichess Broadcast
OTB Broadcast only; do not ingest normal Lichess user games.
Hot priority: 46th FIDE Chess Olympiad Samarkand 2026.
Verified 2026-09-20: Lichess has live split Open/Women broadcast feeds.
Repeated round refreshes must be idempotent and retain raw provenance.

### Chess.com / Chess24
OTB event/broadcast only; no ordinary online player archives.
The 2026 Olympiad is currently broadcast there. Prefer an explicit PGN
or structured event game endpoint; do not scrape visual boards if a
structured feed exists.
## Critical path: B2b workload packaging

Existing implementation plan:
`docs/superpowers/plans/2026-09-14-b2b-workload-packaging.md`

Deliver local B2b before waiting for TWIC/Lichess/Chess.com.

Required contract:

- `cgm-tournament-1` manifest
- `cgm-job-1` JobSpec
- `whole_game_plies_v1`
- target ~3,000 plies/shard
- never split a game
- deterministic bytes/hashes/keys
- provider-neutral JobSpec

Frozen B1 contract remains:
Stockfish 19, pipeline v3, depth 19, time 0, MultiPV 1,
snapshots 12/14/16/18/19, scheduler `game_affinity_lpt`,
UCI archive enabled.
Recommended B2b order:

1. manifests + frozen analysis contract
2. deterministic whole-game sharding
3. Registry packaging DTO/read APIs
4. local PackageService
5. local `cgm-package` CLI
6. prove one emitted shard imports through existing B1 `import_pgn()`
7. S3-compatible ObjectStore
8. local production acceptance
9. batch package canonical revisions

JobSpec must not contain provider, workers, threads, Hash, credentials,
engine binary path, or notebook/cloud paths.

## Compute/control-plane targets

All providers consume the same JobSpec/shard and differ only in runtime
configuration.

### Oracle Cloud — coordinator first, worker second

Oracle is the always-on **control plane / coordinator**. It is not the
primary bulk Stockfish machine.

Validated host on 2026-09-21:

- VM.Standard.A1.Flex
- 2 OCPU, ARM64 Neoverse-N1
- 12 GB RAM
- Ubuntu 24.04.4 LTS
- ~184 GB root filesystem
- Desktop Commander reconnects automatically after reboot
- Stockfish 19 ARM64 is installed and verified

Primary Oracle responsibilities:

- run scheduled/bounded source ingestion for Chess-Results, Lichess
  Broadcast, Chess.com/Chess24 and TWIC;
- keep a durable local dispatch queue;
- package new/changed canonical revisions into provider-neutral B2b jobs;
- assign/export jobs to Kaggle, Deepnote, Molab/Marimo, Codespaces and
  other headless workers;
- track lease/attempt/status/result metadata;
- receive result bundles back;
- verify checksums and job/config identity;
- retain a durable local copy of accepted results and dispatch state;
- surface failed/stale jobs for retry.

Oracle may also execute B1 jobs itself, but initially reserve it for
small/high-value/time-sensitive work such as live Olympiad rounds or
when another worker is unavailable.

Initial Oracle B1 runtime profile when used as a worker:

- 2 workers
- 1 Stockfish thread per worker
- 1536 MB Hash per worker
- depth 19

Do not run multiple CPU-heavy analysis jobs concurrently on this 2-OCPU VM.

### Storage/transport policy for the first distributed cycle

Do **not** make S3 a prerequisite for the first distributed cycle.

Start with an explicit local Oracle queue and portable job/result bundles.
The coordinator must separate queue semantics from transport semantics so
we can use whichever transfer path is available for each worker.

Required first transport abstraction:

- export one immutable job bundle to a filesystem path;
- import one result bundle from a filesystem path;
- verify all checksums before accepting it;
- preserve job/attempt identity;
- support manual upload/download or a platform-specific wrapper without
  changing JobSpec.

S3-compatible ObjectStore remains a later transport/backend option, not
the current critical path. Do not put cloud credentials or transport
details into JobSpec.

### Deepnote
Existing conservative profile: 2 workers x 1 thread x 768 MB Hash.

### Kaggle
Existing conservative profile: 4 workers x 1 thread x 1536 MB Hash.

### Molab / Marimo
Existing conservative profile: 4 workers x 1 thread x 1536 MB Hash.
Keep Marimo thin: obtain job -> invoke B1 -> persist/upload outputs.

### Codespaces fallback
Existing profile: 2 workers x 1 thread x 1024 MB Hash.

Runtime workers/threads/Hash stay outside JobSpec/config_hash.
## Parallel execution split

### Agent A — B2b local critical path
Implement manifests, sharding, Registry packaging API, PackageService,
local CLI and B1 import compatibility. No provider-specific compute code.

### Agent B — live source adapters
Priority order:
1. Lichess Olympiad Broadcast
2. Chess.com/Chess24 Olympiad structured feed
3. TWIC issue 1660+ bulk adapter

Use temp registries for development. Never mutate production corpus
during adapter tests.

### Agent C — coordinator + compute deployment
Build the provider-neutral coordinator/dispatch scaffolding around B2b.

Priority:

1. Oracle local durable queue/state + filesystem export/import bundles.
2. Result checksum/identity verification and accepted-result archive.
3. Thin worker contract/wrapper shared by all platforms.
4. Kaggle dispatch/runner path.
5. Deepnote dispatch/runner path.
6. Molab/Marimo dispatch/runner path.
7. Codespaces fallback dispatch/runner path.
8. Oracle worker path for urgent/small jobs.

Do not require S3 for this phase. Keep transport pluggable so S3 can be
added later without changing JobSpec or analysis semantics.

Do not change B1 chess semantics and do not start a large production
Stockfish run before B2b acceptance.

## Integration order

1. Integrate B2b local path.
2. Package one small production revision.
3. Import one shard into B1 without Stockfish.
4. Bring up Oracle coordinator local queue + filesystem bundle export/import.
5. Run ONE packaged shard on a worker path and return its result bundle.
   Oracle itself is acceptable for this acceptance only; it is not the
   intended primary bulk worker.
6. Verify result mapping, checksums and canonical fingerprints on Oracle.
7. Bring Kaggle/Deepnote/Molab-Marimo/Codespaces worker wrappers online.
8. Integrate live Olympiad Lichess/Chess.com feeds as adapters become green.
9. Add TWIC 1660+ bulk ingestion.
10. Dispatch only new/changed jobs and collect results centrally on Oracle.
11. Add S3-compatible transport later if/when it materially simplifies
    distribution or retention.

Because the Olympiad is live now, source capture should proceed in
parallel with B2b, but B2b remains the main analysis critical path.

## Next milestone

```text
one canonical production revision
-> deterministic B2b shards + JobSpecs
-> Oracle durable dispatch queue
-> one shard exported to a worker
-> analysis.sqlite + raw UCI + PGN outputs returned
-> Oracle verifies bundle + maps it to canonical fingerprints
```

After that milestone, scale horizontally to Kaggle, Deepnote,
Molab/Marimo, Codespaces and other headless workers without redesigning
B1, B2a or JobSpec. Oracle remains the 24/7 coordinator and only an
optional/urgent B1 worker.
