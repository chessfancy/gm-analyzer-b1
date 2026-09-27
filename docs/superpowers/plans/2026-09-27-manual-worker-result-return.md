# Manual Worker Result Return Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce coordinator-compatible manual Deepnote/Molab batches and import their returned results safely.

**Architecture:** Oracle creates a batch of existing immutable coordinator job bundles. A provider-neutral worker runner executes each job through the existing `run_job_bundle` contract and packages one result ZIP. Oracle bulk-imports the ZIP through the existing coordinator verifier.

**Tech Stack:** Python 3.12, zipfile, hashlib, existing Coordinator/validate_job_bundle/validate_result_bundle, pytest, Marimo notebook.

**Spec:** `docs/superpowers/specs/2026-09-27-manual-worker-result-return-design.md`

## Global Constraints
- No public unauthenticated result-upload endpoint.
- Inner result bundles remain `cgm-job-result-1`.
- Molab runtime profile is `molab-marimo`; Deepnote runtime profile is `deepnote`.
- Batch import must be idempotent and reject unsafe ZIP paths.
- Do not silently convert old aggregate analysis into per-job completion.

## Review Focus
- Partial/restarted manual runs preserve valid completed inner results.
- Tampered or wrong-job result never completes a coordinator job.
- ZIP path traversal is rejected before extraction.
- Already-completed results import idempotently.
- Expired manual leases are not auto-recovered before their returned archive is handled.

---

### Task 1: Manual batch archive contract
**Files:** create `src/chessgrandmaster/coordinator/manual_batch.py`; test `tests/coordinator/test_manual_batch.py`.
**Interfaces:** create/export/import archive helpers with typed summaries.
- [ ] Write failing tests for archive creation, safe extraction, and bulk import.
- [ ] Verify RED; implement minimal helpers; verify GREEN.

### Task 2: Portable manual worker runner
**Files:** create `scripts/workers/run_manual_batch.py`; extend `tests/coordinator/test_manual_batch.py`.
**Interfaces:** `run_manual_batch(batch_dir, result_dir, executor=execute_job)` and ZIP packaging.
- [ ] Write failing tests for execution and resume.
- [ ] Verify RED; implement; verify GREEN.

### Task 3: Oracle CLI and Molab integration
**Files:** create `scripts/oracle/manual_batch.py`; modify `notebooks/molab_b1.py`, `tests/test_molab_notebook.py`, `scripts/oracle/README.md`.
**Interfaces:** `create`, `import`, and notebook download/run/package flow.
- [ ] Write failing CLI/notebook contract tests.
- [ ] Verify RED; implement; verify GREEN.
- [ ] Run full pytest and an Oracle fixture end-to-end create→execute→import smoke test.
