# Oracle control-plane scripts

These scripts are for the always-on `Oracle-Chess` coordinator.

## Scheduled ingestion

- `refresh_lichess_olympiad.sh`
  - lock-protected;
  - discovers the configured Olympiad broadcast root;
  - acquires each live Lichess round/feed into the authoritative B2a registry;
  - packages the latest revision;
  - idempotently registers B2b shards in the coordinator queue.

- `refresh_chess_results.sh`
  - lock-protected;
  - runs the 2026 Chess-Results corpus refresh;
  - processes newly downloaded sources into canonical revisions.

TWIC scheduling remains disabled until per-game time-control filtering is
implemented. Chess.com remains deferred.

## External-worker credentials

Run this interactively over SSH, never through chat:

```bash
cd ~/projects/gm-analyzer-b1
./scripts/oracle/setup_worker_secrets.sh
```

It stores:

- Kaggle API access token in `~/.kaggle/access_token`
- Deepnote API key in `~/.config/cgm/secrets/deepnote_api_key`

Both files are mode 0600 and are outside the repository.

Then verify both credentials without printing them:

```bash
cd ~/projects/gm-analyzer-b1
./scripts/oracle/probe_worker_auth.py
```

Kaggle authentication uses the current API-token mechanism supported by
the Kaggle CLI. Deepnote verification uses the v2 API.

Do not commit, print, or copy credential values into logs, JobSpec,
manifests, result bundles, or chat.

## Reusable dispatchers

Each dispatcher performs exactly one coordinator lease per invocation.

Kaggle:

```bash
cd ~/projects/gm-analyzer-b1
PYTHONPATH=src .venv/bin/python scripts/oracle/kaggle_dispatch.py
```

Deepnote:

```bash
cd ~/projects/gm-analyzer-b1
PYTHONPATH=src .venv/bin/python scripts/oracle/deepnote_dispatch.py
```

Both follow the same contract:

```text
lease
-> export immutable B2b job bundle
-> provider execution
-> download portable result bundle
-> coordinator checksum/identity validation
-> import_result()
-> COMPLETED
```

Provider/runtime metadata never changes the immutable JobSpec.

Kaggle creates one private temporary dataset and one private kernel per
attempt. They are deleted after a successful result import unless
`--keep-remote` is supplied.

Deepnote uploads one attempt-scoped job bundle, runs the
`CGM_Distributed_Worker` notebook detached, then downloads the result
ZIP. Submission is locally serialized so concurrent Oracle dispatchers
cannot race while updating the shared notebook block. Each remote run
also uses an attempt-specific runtime directory.

Provider failures are returned to the coordinator as `RETRY_PENDING`
when possible; failed result identity/checksum validation is never
silently accepted.

Useful options:

```text
--poll-seconds N
--timeout-seconds N
--keep-remote
```

The portable worker entrypoint used by both providers is:

```text
scripts/workers/run_job_bundle.py
```

It produces:

- `analysis.sqlite`
- `Mistakes.pgn`
- `Blunders.pgn`
- raw UCI archive
- `job-result.json`
- `checksums.json`

## Continuous general backfill refill

`refill_backfill_queue.py` bridges the authoritative B2 registry to the
distributed coordinator after urgent Olympiad work is exhausted. By default it
only selects canonical Chess-Results tournaments, refills when fewer than 8
priority-500 jobs remain, and fills to at least 24 jobs using 1800-ply shards.

```bash
cd ~/projects/gm-analyzer-b1
PYTHONPATH=src .venv/bin/python scripts/oracle/refill_backfill_queue.py
```

`run_backfill_refill.sh` wraps the command with `flock` for cron. Existing
Kaggle/Codespaces dispatchers need no source-specific changes: they lease the
new priority-500 jobs through the same immutable coordinator contract. Higher
Olympiad priorities continue to preempt general backfill naturally.

## Manual Deepnote / Molab result round-trip

Manual production runs use job bundles, not one concatenated PGN. Create and
publish a batch on Oracle:

```bash
PYTHONPATH=src .venv/bin/python scripts/oracle/manual_batch.py create molab --max-jobs 8
PYTHONPATH=src .venv/bin/python scripts/oracle/manual_batch.py create deepnote --max-jobs 8
```

The command publishes `molab-batch.zip` or `deepnote-batch.zip` plus a SHA256
file under `~/data/cgm/distribution`. On the remote worker, run:

```bash
python scripts/workers/run_manual_batch.py \
  --batch-archive manual-batch.zip \
  --result manual-results \
  --archive manual-results.zip
```

The runner is resumable and produces one verified `cgm-job-result-1` bundle
per coordinator job. Copy the single result ZIP back to Oracle and import it:

```bash
PYTHONPATH=src .venv/bin/python scripts/oracle/manual_batch.py import /path/to/manual-results.zip
```

Oracle verifies the outer archive plus every inner checksum, job id, config
hash and input identity before marking a job `COMPLETED`. No unauthenticated
HTTP result-upload endpoint is exposed. Legacy flat-PGN manual outputs are not
silently converted into completed jobs; recover them only after their actual
files are available for a separately verified conversion.
