# Oracle Coordinator / Distributed Worker Plan

Date: 2026-09-21
Branch: `chatgpt-work`

## Role split

Oracle is the 24/7 coordinator and ingestion/control-plane host.
It is not the primary bulk Stockfish compute node.

Oracle responsibilities:

- scheduled source ingestion from Chess-Results, Lichess Broadcast,
  Chess.com/Chess24 and TWIC;
- canonicalization of newly fetched source material;
- deterministic B2b packaging;
- durable local job queue;
- worker assignment and retry state;
- export of immutable job bundles;
- import and verification of result bundles;
- accepted-result archive;
- urgent/small B1 analysis when low latency matters.

Bulk analysis should prefer:

- Kaggle
- Deepnote
- Molab/Marimo
- GitHub Codespaces
- future headless compute providers

Oracle can run B1 when useful, especially for live Olympiad rounds, but
its 2 OCPU should not be consumed as the default bulk engine farm.

## Phase 1: local coordinator, no S3 dependency

S3 is explicitly deferred.

The first coordinator implementation should use only local durable
filesystem + SQLite state on Oracle and portable bundles.

Suggested Oracle layout:

```text
~/data/cgm/
  inbox/
  jobs/
    pending/
    leased/
    completed/
    failed/
  exports/
  imports/
  results/
  archive/
  logs/
  coordinator.sqlite
```

The exact directory names may differ if existing repo conventions are
stronger, but the state machine should remain explicit.

## Job state model

Minimum coordinator job states:

```text
PENDING
-> LEASED
-> RUNNING/EXPORTED
-> RESULT_RECEIVED
-> VERIFIED
-> COMPLETED
```

Failure/recovery states:

```text
FAILED
STALE
RETRY_PENDING
```

A lease/attempt is separate from the immutable JobSpec.

Each attempt records at least:

- job_id
- worker/provider
- attempt number
- created/leased/finished timestamps
- transport/export reference
- result import reference
- status
- failure reason

Do not place provider or attempt data inside `cgm-job-1`.

## Portable job bundle

First version should be a deterministic directory or archive containing:

```text
job.json
manifest.json
input.pgn
checksums.json
```

Optional future metadata files may be added outside the JobSpec.

The bundle must be independently verifiable before a worker starts.

## Portable result bundle

A worker returns a bundle containing at least:

```text
job-result.json
analysis.sqlite
Mistakes.pgn
Blunders.pgn
raw-uci archive/files
checksums.json
```

The exact raw-UCI layout should follow the frozen B1 output contract.

Oracle must reject a result if:

- job_id does not match;
- config/analysis contract does not match;
- expected shard/input identity does not match;
- a declared checksum fails;
- required B1 outputs are missing.

Rejected results are preserved for debugging and are not marked complete.

## Transport abstraction

Coordinator logic must not assume S3.

Minimum transport API conceptually:

```text
export_job(job_id, destination)
import_result(path/reference)
```

Initial transport:

```text
filesystem/manual bundle transfer
```

Platform-specific adapters may later automate transfer:

- Kaggle CLI/API
- Deepnote runner/API where supported
- Molab/Marimo notebook upload/download flow
- GitHub Codespaces/gh workflow
- Oracle local execution

S3-compatible storage can later become another transport/backend without
changing JobSpec, queue semantics or worker result format.

## Platform worker contract

Every worker wrapper should do the same logical sequence:

```text
1. obtain job bundle
2. verify input checksums
3. materialize isolated work directory
4. invoke frozen B1 with platform runtime settings
5. verify required outputs exist
6. build result bundle + checksums
7. return/export result
8. exit
```

No worker is allowed to mutate the Oracle canonical registry.

## Runtime profiles

Keep current profiles outside JobSpec.

Oracle A1:
- 2 workers
- 1 thread
- 1536 MB Hash
- depth 19
- one B1 job at a time

Deepnote:
- 2 workers
- 1 thread
- 768 MB Hash

Kaggle:
- 4 workers
- 1 thread
- 1536 MB Hash

Molab/Marimo:
- 4 workers
- 1 thread
- 1536 MB Hash

Codespaces:
- 2 workers
- 1 thread
- 1024 MB Hash

Do not rerun Stockfish benchmarking for this coordinator work.

## Live-source priority

While B2b/coordinator work is being implemented, ingestion work proceeds
in parallel.

Priority:

1. Lichess Broadcast — 46th FIDE Chess Olympiad 2026
2. Chess.com/Chess24 structured Olympiad feed
3. TWIC issue 1660 onward
4. recurring Chess-Results refresh

New provider occurrences must dedupe through the existing canonical game
identity while preserving provenance.

## First distributed acceptance

Do not start bulk analysis first.

Acceptance:

```text
one production canonical revision
-> one deterministic B2b shard/job
-> Oracle registers PENDING
-> export bundle
-> one worker executes it
-> result bundle returned to Oracle
-> checksum/identity verification
-> COMPLETED
-> canonical-game mapping audited
```

The worker may be Oracle itself for this first acceptance if that is the
fastest route, but this does not make Oracle the primary production
analyzer.

## Scaling rule

Only after one end-to-end result is accepted should we enable multiple
workers/platforms.

The coordinator should prefer idle external compute for bulk work and
reserve Oracle compute for:

- fresh live rounds;
- small priority tournaments;
- retries requiring immediate attention;
- visual/post-processing stages that later benefit from an always-on host.

## Agent execution split

Agent A:
- complete B2b local packaging contract.

Agent B:
- implement live Lichess/Chess.com adapters, then TWIC.

Agent C:
- implement coordinator queue, bundle export/import, result verification
  and thin platform wrappers.

Lead executor integrates the three streams on `chatgpt-work`, runs the
normal regression suite, pushes, then stops for external audit.
