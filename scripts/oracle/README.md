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
the Kaggle CLI. Deepnote verification uses `GET https://api.deepnote.com/v2/me`
and lists accessible projects through the v2 API.

Do not commit, print, or copy the credential values into logs, JobSpec,
manifests, result bundles, or chat.
