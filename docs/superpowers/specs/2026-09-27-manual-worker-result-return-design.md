# Manual Worker Result Return Design

## Goal
Make manual Deepnote and Molab work coordinator-native: workers consume immutable job bundles and return one portable archive that Oracle can verify/import job-by-job.

## Current gap
The current manual workload is one concatenated PGN. Molab runs `cgm-analyze` on it, producing one aggregate database rather than `cgm-job-result-1` bundles, so coordinator `import_result()` cannot accept the finished work.

## Design
A manual batch contains `batch.json` plus individual exported coordinator job bundles under `jobs/NNNN/`. The worker runner validates every job bundle and invokes the existing `run_job_bundle.execute_job()` with the canonical runtime profile (`deepnote` or `molab-marimo`). Results are written under `results/NNNN/`, each already carrying `job-result.json`, `analysis.sqlite`, PGNs, raw UCI, and checksums.

The runner is resumable: a valid existing result is skipped. After all jobs finish it creates one ZIP archive plus SHA256. Oracle imports that archive safely, rejects path traversal, validates every inner result against the registered job, calls `Coordinator.import_result()`, and reports completed/already-completed/rejected counts.

Transport remains intentionally manual and provider-neutral: one archive is downloaded/copied from Deepnote or Molab to Oracle. We do not expose an unauthenticated HTTP upload endpoint. A future HTTPS/S3 transport can replace the file transfer without changing the batch/result contract.

For future batches Oracle leases and exports individual jobs. Existing old aggregate Molab/Deepnote outputs are not falsely marked complete; they require either a one-time proven converter after files are available or rerun through this native batch contract.

## Tests
Tests cover batch creation, worker result generation with a fake executor, resumability, ZIP traversal rejection, checksum verification, and bulk coordinator import/idempotence.
