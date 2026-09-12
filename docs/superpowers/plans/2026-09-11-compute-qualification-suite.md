# Compute Qualification Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a repeatable benchmark that measures whether a compute platform is suitable for ChessGrandmaster analysis at depth 18, 19, or 20.

**Architecture:** The suite benchmarks Stockfish with one thread and MultiPV=1. It separates fixed-work nodes benchmarking, time-to-depth benchmarking, and a Lucas-compatible real-pipeline sample. It reports hardware capacity and estimates tournament throughput without changing production analysis policy.

**Tech Stack:** Python 3.11+, python-chess, Stockfish, argparse, stdlib statistics/json/platform.

**Spec:** Approved in chat on 2026-09-11.

## Global Constraints

- Do not change production search policy.
- Qualification uses one Stockfish thread.
- Fixed-node work is for hardware comparison only.
- Depth benchmarking tests depth 18, 19, and 20.
- Pipeline benchmark must include Lucas second-search behavior.
- Hardware detection should respect Linux cgroup CPU and memory limits where possible.
- Benchmark output must be machine-readable JSON as well as human-readable text.
- Fast pytest suite must not launch Stockfish.

---

### Task 1: Qualification primitives

Create `src/chessgrandmaster/benchmark.py`.

Implement:
- percentile calculation
- PGN position sampling
- CPU cgroup detection
- memory cgroup detection
- depth recommendation from a configurable p95 target

Test in `tests/test_benchmark.py`.

### Task 2: Stockfish benchmark phases

Implement:
- fixed-work nodes phase
- raw depth 18/19/20 phase
- full Lucas-compatible pipeline at depth 18/19/20
- production recommendation from full-pipeline P95, not raw search P95
- per-depth tournament capacity estimates

Use:
- Threads=1
- MultiPV=1
- Hash=256 MB by default

### Task 3: CLI

Add:

    cgm-bench qualify

Options include:
- --pgn
- --samples
- --fixed-nodes
- --depths
- --hash-mb
- --target-p95
- --tournament-moves
- --engine
- --json-out

### Task 4: Verification

Run:
- RED test
- GREEN benchmark tests
- complete fast pytest suite
- real Stockfish qualification against the golden PGN
- git diff --check
- commit and push
