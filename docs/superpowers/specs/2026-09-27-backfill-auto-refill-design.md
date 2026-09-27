# Backfill Auto-Refill Design

## Goal
Keep Kaggle and Codespaces supplied with general backfill work after high-priority Olympiad jobs are exhausted, without changing the immutable job contract.

## Current gap
The 2026 registry has many CANONICALIZED tournaments, but only the Olympiad refresh packages/registers work. Chess-Results refresh stops after canonicalization, so worker slots idle when no priority >=500 job exists.

## Design
Add a lock-safe Oracle feeder that runs independently from provider dispatchers. It observes eligible queue depth, and only refills when it drops below a low watermark. It packages deterministic latest revisions and idempotently registers enough shards to reach a high watermark.

Initial candidate scope is Chess-Results tournaments in CANONICALIZED/READY state. This avoids accidentally packaging live/incomplete broadcast revisions. Candidate order is deterministic by registry tournament id; registry priority enrichment is currently all zero, so no pretend ranking is introduced.

General backfill jobs use priority 500 and target 1800 plies. Existing Olympiad jobs remain >700 and therefore preempt general work naturally. Default watermarks are 8/24 eligible jobs.

The feeder never executes Stockfish and never mutates result state. Existing Kaggle/Codespaces slot scripts keep their current concurrency limits (2 Kaggle slots, 1 Codespaces slot) and quota/provider failures remain handled by their dispatchers.

## Idempotence and recovery
Packaging is deterministic and coordinator registration is idempotent. A tournament revision already represented in the coordinator is skipped. A partial feeder rerun can safely package/register again.

## Tests
Tests cover low-watermark refill, no-op above watermark, already-registered revision skipping, source filtering, and deterministic selection.
