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

## Compute targets

All providers consume the same JobSpec/shard and differ only in runtime
configuration.

### Oracle Cloud
New first always-on worker target after B2b acceptance.
Record instance shape, CPU architecture/vCPU, RAM, OS, disk and object
store/network access before deployment. Do not rerun Stockfish
benchmarks merely to choose settings.
Oracle worker flow:

JobSpec + shard PGN
-> frozen B1 analysis
-> analysis.sqlite + raw UCI archive
-> Mistakes.pgn + Blunders.pgn
-> checksum result bundle
-> upload/collect with job id + config hash

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

### Agent C — compute deployment
Prepare Oracle Cloud first, then Deepnote/Kaggle/Molab-Marimo wrappers.
Do not change B1 chess semantics and do not start a large production
Stockfish run before B2b acceptance.

## Integration order

1. Integrate B2b local path.
2. Package one small production revision.
3. Import one shard into B1 without Stockfish.
4. Add/verify S3-compatible storage.
5. Run ONE packaged shard on Oracle Cloud.
6. Verify result mapping to canonical fingerprints.
7. Scale analysis to Oracle + Deepnote + Kaggle + Molab/Marimo.
8. Integrate live Olympiad Lichess/Chess.com feeds as adapters become green.
9. Add TWIC 1660+ bulk ingestion.
10. Package only new/changed revisions and feed the same analysis queue.

Because the Olympiad is live now, source capture should proceed in
parallel with B2b, but B2b remains the main analysis critical path.

## Next milestone

```text
one canonical production revision
-> deterministic B2b shards + JobSpecs
-> one shard executed on Oracle Cloud
-> analysis.sqlite + raw UCI + PGN outputs verified
-> result maps back to canonical fingerprints
```

After that milestone, scale horizontally without redesigning B1 or B2a.
