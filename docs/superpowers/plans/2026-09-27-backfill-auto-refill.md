# Backfill Auto-Refill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Continuously refill the coordinator from canonical Chess-Results tournaments when production queue depth is low.

**Architecture:** A testable refill module reads Registry and Coordinator state, packages deterministic latest revisions, and idempotently registers shards. A lock-protected Oracle script runs it on cron; existing provider dispatchers consume the jobs unchanged.

**Tech Stack:** Python 3.12, sqlite3, existing Registry/PackageService/Coordinator, pytest, cron/flock.

**Spec:** `docs/superpowers/specs/2026-09-27-backfill-auto-refill-design.md`

## Global Constraints
- General backfill priority: 500.
- Default queue watermarks: refill below 8, fill to at least 24 eligible jobs.
- Default shard target: 1800 plies.
- Initial source filter: `chess-results`.
- Do not alter immutable JobSpec or existing Olympiad priority behavior.

## Review Focus
- READY tournament with no coordinator jobs must still be refillable.
- Existing revision must not duplicate jobs.
- Queue at/above low watermark must not package anything.
- Non-Chess-Results tournament must be skipped by default.
- One tournament producing more shards than the remaining gap is acceptable and deterministic.

---

### Task 1: Coordinator refill service
**Files:** create `src/chessgrandmaster/coordinator/refill.py`; test `tests/coordinator/test_refill.py`.
**Interfaces:** produce `refill_backfill_queue(...) -> RefillSummary`.
- [ ] Write failing tests for selection, watermarks, idempotence and source filtering.
- [ ] Run tests and verify RED.
- [ ] Implement the minimal refill service.
- [ ] Run focused tests and verify GREEN.

### Task 2: Oracle scheduler entrypoint
**Files:** create `scripts/oracle/refill_backfill_queue.py`, `scripts/oracle/run_backfill_refill.sh`; update `scripts/oracle/README.md`.
**Interfaces:** CLI prints one JSON summary; shell wrapper uses flock.
- [ ] Write failing CLI/script contract tests.
- [ ] Verify RED, implement, then verify GREEN.
- [ ] Dry-run against Oracle production paths without registering when queue is above low watermark.
