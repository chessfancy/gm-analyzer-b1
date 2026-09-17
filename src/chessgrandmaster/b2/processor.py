"""Parallel, local-only processing of immutable B2 raw PGN objects."""

from __future__ import annotations

from collections import deque
from contextlib import redirect_stderr
from dataclasses import dataclass, replace
from functools import partial
import io
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import re
import time
from typing import Callable, Iterable, Mapping

import chess.pgn

from .canonicalize import (
    ProcessedGameResult,
    _stable_error,
    finalize_processed_games,
)
from .registry import Registry
from .storage import ObjectStore


_TAG_LINE = re.compile(
    r'^\s*\[([A-Za-z][A-Za-z0-9_]*)\s+"((?:\\.|[^"\\])*)"\]\s*(?:\r?\n)?$'
)
_TIMING_STAGES = (
    "restore_checksum",
    "segmentation",
    "queue_start_wait",
    "parse",
    "identity_replay",
    "sqlite_write",
    "metadata_display_selection",
    "projection_export",
    "revision_finalization",
    "total_source",
)


@dataclass(frozen=True)
class SourceGameSlice:
    """One deterministic raw-text slice submitted to exactly one worker."""

    source_game_index: int
    raw_text: str
    raw_headers: dict[str, str]


@dataclass(frozen=True)
class ProcessGameJob:
    """Picklable worker input; it contains no Registry or object-store handle."""

    source_game_index: int
    raw_text: str
    raw_headers: dict[str, str]


WorkerOperation = Callable[[ProcessGameJob], ProcessedGameResult]


@dataclass(frozen=True)
class PoolStats:
    """Parent-observable evidence about worker replacement and termination."""

    replacements: int = 0
    terminated_worker_pids: tuple[int, ...] = ()
    terminated_worker_alive: tuple[bool, ...] = ()


@dataclass(frozen=True)
class SourceProcessResult:
    tournament_id: int
    source_file_id: int | None
    raw_sha256: str | None
    action: str
    status: str
    source_game_count: int = 0
    valid_game_count: int = 0
    invalid_game_count: int = 0
    timed_out_game_count: int = 0
    canonical_game_count: int = 0
    canonical_ply_count: int = 0
    revision_id: int | None = None
    revision_number: int | None = None
    canonical_sha256: str | None = None
    canonical_path: Path | None = None
    error_type: str | None = None
    error_message: str | None = None
    duplicate_occurrence_count: int = 0
    metadata_conflict_count: int = 0
    timing: dict[str, dict[str, int | float]] | None = None
    worker_replacements: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "tournament_id": self.tournament_id,
            "source_file_id": self.source_file_id,
            "raw_sha256": self.raw_sha256,
            "action": self.action,
            "status": self.status,
            "source_game_count": self.source_game_count,
            "valid_game_count": self.valid_game_count,
            "invalid_game_count": self.invalid_game_count,
            "timed_out_game_count": self.timed_out_game_count,
            "canonical_game_count": self.canonical_game_count,
            "canonical_ply_count": self.canonical_ply_count,
            "revision_id": self.revision_id,
            "revision_number": self.revision_number,
            "canonical_sha256": self.canonical_sha256,
            "canonical_path": (
                None if self.canonical_path is None else str(self.canonical_path)
            ),
            "error_type": self.error_type,
            "error_message": self.error_message,
            "duplicate_occurrence_count": self.duplicate_occurrence_count,
            "metadata_conflict_count": self.metadata_conflict_count,
            "timing": self.timing or _empty_timings(),
            "worker_replacements": self.worker_replacements,
        }


@dataclass(frozen=True)
class ProcessReport:
    ok: bool
    selected_tournaments: int
    processed_tournaments: int
    canonicalized: int
    review_required_sources: int
    failed_sources: int
    source_games: int
    valid_games: int
    invalid_games: int
    timed_out_games: int
    duplicate_occurrences: int
    metadata_conflicts: int
    workers: int
    game_timeout_sec: float
    timings: dict[str, dict[str, int | float]]
    results: tuple[SourceProcessResult, ...]
    worker_replacements: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "selected_tournaments": self.selected_tournaments,
            "processed_tournaments": self.processed_tournaments,
            "canonicalized": self.canonicalized,
            "review_required_sources": self.review_required_sources,
            "failed_sources": self.failed_sources,
            "source_games": self.source_games,
            "valid_games": self.valid_games,
            "invalid_games": self.invalid_games,
            "timed_out_games": self.timed_out_games,
            "duplicate_occurrences": self.duplicate_occurrences,
            "metadata_conflicts": self.metadata_conflicts,
            "workers": self.workers,
            "game_timeout_sec": self.game_timeout_sec,
            "worker_replacements": self.worker_replacements,
            "timings": self.timings,
            "results": [result.to_dict() for result in self.results],
        }


def resolve_worker_count(workers: str | int) -> int:
    """Resolve ``auto`` as max(1, min(8, (cpu_count or 2) - 1))."""
    if isinstance(workers, str):
        value = workers.strip().casefold()
        if value == "auto":
            cpu_count = os.cpu_count() or 2
            return max(1, min(8, cpu_count - 1))
        try:
            workers = int(value)
        except ValueError as exc:
            raise ValueError("workers must be auto or a positive integer") from exc
    if int(workers) < 1:
        raise ValueError("workers must be auto or a positive integer")
    return int(workers)


def _unescape_tag_value(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            index += 1
            escaped = value[index]
            result.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
        else:
            result.append(character)
        index += 1
    return "".join(result)


@dataclass
class _LexicalState:
    brace_depth: int = 0
    variation_depth: int = 0
    bracket_depth: int = 0
    quote: bool = False


def _tag_line(line: str) -> tuple[str, str] | None:
    match = _TAG_LINE.match(line)
    if match is None:
        return None
    return match.group(1), _unescape_tag_value(match.group(2))


def _scan_line(line: str, state: _LexicalState) -> None:
    """Advance a small PGN lexical scanner without parsing chess content."""
    line_comment = False
    index = 0
    while index < len(line):
        character = line[index]
        if line_comment:
            break
        if state.brace_depth:
            if character == "\\" and index + 1 < len(line):
                index += 2
                continue
            if character == "{":
                state.brace_depth += 1
            elif character == "}":
                state.brace_depth -= 1
            index += 1
            continue
        if state.quote:
            if character == "\\" and index + 1 < len(line):
                index += 2
                continue
            if character == '"':
                state.quote = False
            index += 1
            continue
        if state.bracket_depth:
            if character == '"':
                state.quote = True
            elif character == "]":
                state.bracket_depth -= 1
            index += 1
            continue
        if character == ";":
            line_comment = True
        elif character == "{":
            state.brace_depth += 1
        elif character == "(":
            state.variation_depth += 1
        elif character == ")":
            state.variation_depth = max(0, state.variation_depth - 1)
        elif character == "[":
            state.bracket_depth = 1
        index += 1


def _looks_like_terminator(line: str) -> bool:
    return bool(re.search(r"(?:1-0|0-1|1/2-1/2|\*)\s*$", line.strip()))


def _looks_like_header_block(lines: list[str], index: int) -> bool:
    tag_count = 0
    for line in lines[index + 1 :]:
        if not line.strip():
            continue
        if _tag_line(line) is None:
            break
        tag_count += 1
    return tag_count > 0


def _segment_headers(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    header_started = False
    movement_started = False
    for line in lines:
        tag = _tag_line(line)
        if tag is not None and not movement_started:
            headers[tag[0]] = tag[1]
            header_started = True
            continue
        if header_started and line.strip():
            movement_started = True
    return headers


def segment_pgn_text(raw_text: str) -> list[SourceGameSlice]:
    """Segment PGN lexically, preserving source order and raw game text.

    A new top-level Event tag is accepted as a boundary only when it is
    followed by another tag pair or the previous non-empty line has a PGN
    termination marker.  Braced comments, semicolon comments, variations,
    and tag-like text embedded in movetext therefore remain in their slice.
    """
    if not isinstance(raw_text, str):
        raise TypeError("raw PGN text must be a string")
    lines = raw_text.splitlines(keepends=True)
    if not lines:
        return []

    slices: list[SourceGameSlice] = []
    current: list[str] = []
    state = _LexicalState()
    event_seen = False
    movement_seen = False
    previous_nonempty = ""

    for index, line in enumerate(lines):
        tag = _tag_line(line)
        at_top_level = (
            state.brace_depth == 0
            and state.variation_depth == 0
            and state.bracket_depth == 0
            and not state.quote
        )
        is_boundary = (
            tag is not None
            and tag[0].casefold() == "event"
            and at_top_level
            and event_seen
            and movement_seen
            and (
                _looks_like_header_block(lines, index)
                or _looks_like_terminator(previous_nonempty)
            )
        )
        if is_boundary:
            slices.append(
                SourceGameSlice(
                    source_game_index=len(slices) + 1,
                    raw_text="".join(current),
                    raw_headers=_segment_headers(current),
                )
            )
            current = []
            state = _LexicalState()
            event_seen = False
            movement_seen = False
            previous_nonempty = ""

        current.append(line)
        if tag is not None and not movement_seen:
            if tag[0].casefold() == "event":
                event_seen = True
        elif event_seen and line.strip():
            movement_seen = True
            previous_nonempty = line
        _scan_line(line, state)
        if line.strip() and tag is not None:
            previous_nonempty = line

    if current:
        slices.append(
            SourceGameSlice(
                source_game_index=len(slices) + 1,
                raw_text="".join(current),
                raw_headers=_segment_headers(current),
            )
        )
    return slices


def segment_pgn_bytes(raw_bytes: bytes) -> list[SourceGameSlice]:
    """Decode immutable raw bytes with the canonical UTF-8-SIG policy."""
    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw PGN bytes must be bytes")
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("raw PGN must be UTF-8, with an optional BOM") from exc
    return segment_pgn_text(raw_text)


def _invalid_result(
    job: ProcessGameJob,
    error: BaseException | str,
    *,
    parse_ms: float = 0.0,
    identity_ms: float = 0.0,
    parse_count: int = 0,
) -> ProcessedGameResult:
    parse_error = error if isinstance(error, str) else _stable_error(error)
    return ProcessedGameResult(
        source_game_index=job.source_game_index,
        raw_headers=dict(job.raw_headers),
        identity=None,
        is_valid=False,
        parse_error=parse_error,
        parse_ms=parse_ms,
        identity_ms=identity_ms,
        parse_count=parse_count,
    )


def process_game_job(job: ProcessGameJob) -> ProcessedGameResult:
    """Parse one source game once, then compute exact existing identity."""
    parse_started = time.perf_counter()
    parse_count = 1
    try:
        with redirect_stderr(io.StringIO()):
            game = chess.pgn.read_game(io.StringIO(job.raw_text))
    except BaseException as exc:
        parse_ms = (time.perf_counter() - parse_started) * 1000.0
        return _invalid_result(
            job,
            exc,
            parse_ms=parse_ms,
            parse_count=parse_count,
        )
    parse_ms = (time.perf_counter() - parse_started) * 1000.0
    if game is None:
        return _invalid_result(
            job,
            "ProcessingError: no PGN game parsed",
            parse_ms=parse_ms,
            parse_count=parse_count,
        )

    headers = {str(key): str(value) for key, value in game.headers.items()}
    if not headers:
        headers = dict(job.raw_headers)
    if game.errors:
        return ProcessedGameResult(
            source_game_index=job.source_game_index,
            raw_headers=headers,
            identity=None,
            is_valid=False,
            parse_error="; ".join(_stable_error(error) for error in game.errors),
            parse_ms=parse_ms,
            parse_count=parse_count,
        )

    identity_started = time.perf_counter()
    try:
        from .canonicalize import _replay_identity
        from .identity import identify_game

        identity = identify_game(game)
        _replay_identity(identity)
    except Exception as exc:
        identity_ms = (time.perf_counter() - identity_started) * 1000.0
        return _invalid_result(
            ProcessGameJob(job.source_game_index, job.raw_text, headers),
            exc,
            parse_ms=parse_ms,
            identity_ms=identity_ms,
            parse_count=parse_count,
        )
    identity_ms = (time.perf_counter() - identity_started) * 1000.0
    return ProcessedGameResult(
        source_game_index=job.source_game_index,
        raw_headers=headers,
        identity=identity,
        is_valid=True,
        parse_ms=parse_ms,
        identity_ms=identity_ms,
        parse_count=parse_count,
    )


def _delayed_worker_operation(
    job: ProcessGameJob,
    delay_seconds: float,
    delayed_indexes: tuple[int, ...],
) -> ProcessedGameResult:
    if job.source_game_index in delayed_indexes:
        time.sleep(delay_seconds)
    return process_game_job(job)


def delayed_worker_operation(
    delay_seconds: float,
    source_game_indexes: Iterable[int] = (1,),
) -> WorkerOperation:
    """Return a picklable test hook for exercising hard worker termination."""
    if delay_seconds < 0:
        raise ValueError("delay_seconds must not be negative")
    return partial(
        _delayed_worker_operation,
        delay_seconds=float(delay_seconds),
        delayed_indexes=tuple(int(index) for index in source_game_indexes),
    )


def _worker_main(
    command_queue,
    result_queue,
    worker_id: int,
    generation: int,
    operation: WorkerOperation | None,
) -> None:
    while True:
        job = command_queue.get()
        if job is None:
            return
        result_queue.put(
            {
                "kind": "started",
                "worker_id": worker_id,
                "generation": generation,
                "source_game_index": job.source_game_index,
            }
        )
        try:
            result = process_game_job(job) if operation is None else operation(job)
            if not isinstance(result, ProcessedGameResult):
                raise TypeError("worker operation returned an invalid result")
        except BaseException as exc:
            result = _invalid_result(job, exc)
        result_queue.put(
            {
                "kind": "result",
                "worker_id": worker_id,
                "generation": generation,
                "source_game_index": job.source_game_index,
                "result": result,
            }
        )


@dataclass
class _WorkerSlot:
    worker_id: int
    generation: int
    command_queue: object
    process: mp.Process
    active_key: tuple[int, int] | None = None


@dataclass
class _ActiveJob:
    job: ProcessGameJob
    assigned_at: float
    started_at: float | None = None
    queue_wait_ms: float = 0.0


@dataclass(frozen=True)
class _PoolRun:
    results: tuple[ProcessedGameResult, ...]
    timed_out_games: int
    stats: PoolStats


class _PersistentWorkerPool:
    def __init__(self, workers: int, operation: WorkerOperation | None = None):
        self.workers = int(workers)
        self.operation = operation
        self.context = mp.get_context("spawn")
        self.result_queue = self.context.Queue()
        self.slots: dict[int, _WorkerSlot] = {}
        self.active: dict[tuple[int, int], _ActiveJob] = {}
        self.results: dict[int, ProcessedGameResult] = {}
        self.replacements = 0
        self.terminated_worker_pids: list[int] = []
        self.terminated_worker_alive: list[bool] = []
        for worker_id in range(self.workers):
            self.slots[worker_id] = self._spawn_slot(worker_id, 0)

    def _spawn_slot(self, worker_id: int, generation: int) -> _WorkerSlot:
        command_queue = self.context.Queue(maxsize=1)
        process = self.context.Process(
            target=_worker_main,
            args=(
                command_queue,
                self.result_queue,
                worker_id,
                generation,
                self.operation,
            ),
        )
        process.start()
        return _WorkerSlot(worker_id, generation, command_queue, process)

    def _replace_slot(self, slot: _WorkerSlot) -> None:
        try:
            slot.command_queue.close()
        except (AttributeError, OSError):
            pass
        self.replacements += 1
        replacement = self._spawn_slot(slot.worker_id, slot.generation + 1)
        self.slots[slot.worker_id] = replacement

    @staticmethod
    def _terminate_and_join(process: mp.Process) -> bool:
        if process.is_alive():
            process.terminate()
            process.join(timeout=1.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=5.0)
        return process.is_alive()

    def _timeout_slot(
        self,
        slot: _WorkerSlot,
        active_key: tuple[int, int],
        timeout_sec: float,
    ) -> None:
        active = self.active.pop(active_key)
        slot.active_key = None
        still_alive = self._terminate_and_join(slot.process)
        pid = slot.process.pid
        if pid is not None:
            self.terminated_worker_pids.append(int(pid))
            self.terminated_worker_alive.append(still_alive)
        if still_alive:
            raise RuntimeError("timed-out worker process could not be terminated")
        self.results[active.job.source_game_index] = replace(
            _invalid_result(
                active.job,
                f"ProcessingTimeout: exceeded {timeout_sec:.3f}s",
            ),
            queue_wait_ms=active.queue_wait_ms,
        )
        self._replace_slot(slot)

    def _dead_slot(self, slot: _WorkerSlot) -> None:
        active_key = slot.active_key
        if active_key is not None and active_key in self.active:
            active = self.active.pop(active_key)
            slot.active_key = None
            self.results[active.job.source_game_index] = replace(
                _invalid_result(
                    active.job,
                    "ProcessingWorkerDied: worker exited before result",
                ),
                queue_wait_ms=active.queue_wait_ms,
            )
        if not slot.process.is_alive():
            self._replace_slot(slot)

    def _dispatch(self, pending: deque[ProcessGameJob]) -> None:
        for slot in self.slots.values():
            if slot.active_key is not None or not slot.process.is_alive():
                continue
            if not pending:
                return
            job = pending.popleft()
            key = (slot.worker_id, slot.generation)
            slot.command_queue.put(job)
            slot.active_key = key
            self.active[key] = _ActiveJob(job=job, assigned_at=time.perf_counter())

    def _handle_event(self, event: Mapping[str, object]) -> None:
        worker_id = int(event["worker_id"])
        generation = int(event["generation"])
        slot = self.slots.get(worker_id)
        if slot is None or slot.generation != generation:
            return
        key = (worker_id, generation)
        active = self.active.get(key)
        if active is None:
            return
        kind = str(event["kind"])
        if kind == "started":
            if active.started_at is None:
                active.started_at = time.perf_counter()
                active.queue_wait_ms = (active.started_at - active.assigned_at) * 1000.0
            return
        if kind != "result":
            return
        result = event["result"]
        if not isinstance(result, ProcessedGameResult):
            result = _invalid_result(active.job, "ProcessingError: invalid worker result")
        self.results[active.job.source_game_index] = replace(
            result,
            queue_wait_ms=active.queue_wait_ms,
        )
        self.active.pop(key, None)
        slot.active_key = None

    def run(self, jobs: Iterable[ProcessGameJob], timeout_sec: float) -> _PoolRun:
        pending = deque(sorted(jobs, key=lambda job: job.source_game_index))
        self.results = {}
        self.active = {}
        while pending or self.active:
            for slot in list(self.slots.values()):
                if not slot.process.is_alive():
                    self._dead_slot(slot)
            self._dispatch(pending)
            try:
                event = self.result_queue.get(timeout=0.01)
            except queue.Empty:
                event = None
            if event is not None:
                self._handle_event(event)
                while True:
                    try:
                        self._handle_event(self.result_queue.get_nowait())
                    except queue.Empty:
                        break

            now = time.perf_counter()
            for key, active in list(self.active.items()):
                if (
                    active.started_at is not None
                    and now - active.started_at >= timeout_sec
                ):
                    slot = self.slots[active_key_worker(key)]
                    self._timeout_slot(slot, key, timeout_sec)
                    break
        results = tuple(self.results[index] for index in sorted(self.results))
        return _PoolRun(
            results=results,
            timed_out_games=sum(
                1
                for result in results
                if (result.parse_error or "").startswith("ProcessingTimeout:")
            ),
            stats=PoolStats(
                replacements=self.replacements,
                terminated_worker_pids=tuple(self.terminated_worker_pids),
                terminated_worker_alive=tuple(self.terminated_worker_alive),
            ),
        )

    def _shutdown(self) -> None:
        for slot in self.slots.values():
            if slot.process.is_alive():
                try:
                    slot.command_queue.put(None)
                except (OSError, EOFError):
                    pass
        for slot in self.slots.values():
            slot.process.join(timeout=2.0)
            if slot.process.is_alive():
                self._terminate_and_join(slot.process)
            try:
                slot.command_queue.close()
            except (AttributeError, OSError):
                pass
        try:
            self.result_queue.close()
        except (AttributeError, OSError):
            pass

    @property
    def stats(self) -> PoolStats:
        return PoolStats(
            replacements=self.replacements,
            terminated_worker_pids=tuple(self.terminated_worker_pids),
            terminated_worker_alive=tuple(self.terminated_worker_alive),
        )


def active_key_worker(key: tuple[int, int]) -> int:
    return int(key[0])


def select_downloaded_tournaments(registry: Registry) -> list[int]:
    """Return only exact DOWNLOADED statuses in deterministic registry order."""
    return [
        int(row["id"])
        for row in registry.list_tournaments()
        if str(row["status"]) == "DOWNLOADED"
    ]


def select_latest_source_file(
    registry: Registry,
    tournament_id: int,
) -> dict[str, object]:
    """Select latest source bytes using existing timestamp/id precedence."""
    files = registry.list_source_files_for_tournament(int(tournament_id))
    if not files:
        raise ValueError(f"tournament {tournament_id} has no registered source file")

    def sort_key(row: Mapping[str, object]) -> tuple[str, int]:
        timestamp = row.get("downloaded_at") or row.get("created_at") or ""
        return str(timestamp), int(row["id"])

    return max(files, key=sort_key)


class _TimingBook:
    def __init__(self):
        self.values: dict[str, list[float]] = {
            stage: [0.0, 0.0, 0.0] for stage in _TIMING_STAGES
        }

    def add(self, stage: str, elapsed_ms: float, count: int = 1) -> None:
        if stage not in self.values:
            self.values[stage] = [0.0, 0.0, 0.0]
        current = self.values[stage]
        current[0] += float(count)
        current[1] += float(elapsed_ms)
        current[2] = max(current[2], float(elapsed_ms))

    def add_summary(self, stage: str, summary: Mapping[str, object]) -> None:
        count = int(summary.get("count", 0))
        total_ms = float(summary.get("total_ms", 0.0))
        max_ms = float(summary.get("max_ms", 0.0))
        if stage not in self.values:
            self.values[stage] = [0.0, 0.0, 0.0]
        current = self.values[stage]
        current[0] += count
        current[1] += total_ms
        current[2] = max(current[2], max_ms)

    def to_dict(self) -> dict[str, dict[str, int | float]]:
        return {
            stage: {
                "count": int(values[0]),
                "total_ms": round(values[1], 3),
                "max_ms": round(values[2], 3),
            }
            for stage, values in self.values.items()
        }


def _empty_timings() -> dict[str, dict[str, int | float]]:
    return _TimingBook().to_dict()


def _error_fields(error: BaseException) -> tuple[str, str]:
    message = " ".join(str(error).split())
    return type(error).__name__, message or type(error).__name__


class CorpusProcessor:
    """One-shot local processor with a persistent bounded worker pool."""

    def __init__(
        self,
        registry: Registry,
        store: ObjectStore,
        workspace: Path,
        *,
        workers: str | int = "auto",
        game_timeout_sec: float = 2.0,
        worker_operation: WorkerOperation | None = None,
    ):
        self.registry = registry
        self.store = store
        self.workspace = Path(workspace).expanduser()
        self.workers = resolve_worker_count(workers)
        self.game_timeout_sec = float(game_timeout_sec)
        if not math.isfinite(self.game_timeout_sec) or self.game_timeout_sec <= 0:
            raise ValueError("game_timeout_sec must be a finite positive number")
        self.worker_operation = worker_operation
        self.last_pool_stats = PoolStats()

    def _process_tournament(
        self,
        tournament_id: int,
        restore_root: Path,
        pool: _PersistentWorkerPool,
        aggregate_timings: _TimingBook,
    ) -> SourceProcessResult:
        replacements_before = pool.replacements
        source_file = select_latest_source_file(self.registry, tournament_id)
        source_file_id = int(source_file["id"])
        raw_sha256 = str(source_file["sha256"])
        source_timing = _TimingBook()
        total_started = time.perf_counter()
        restore_root.mkdir(parents=True, exist_ok=True)
        raw_path = restore_root / f"{tournament_id}-{source_file_id}.pgn"
        try:
            restore_started = time.perf_counter()
            object_stat = self.store.get_file(str(source_file["object_key"]), raw_path)
            if (
                object_stat.sha256 != raw_sha256
                or object_stat.size != int(source_file["byte_size"])
            ):
                raise ValueError("restored source object failed checksum or size verification")
            raw_bytes = raw_path.read_bytes()
            source_timing.add(
                "restore_checksum",
                (time.perf_counter() - restore_started) * 1000.0,
            )
            aggregate_timings.add_summary(
                "restore_checksum",
                source_timing.to_dict()["restore_checksum"],
            )

            segmentation_started = time.perf_counter()
            segments = segment_pgn_bytes(raw_bytes)
            segmentation_ms = (time.perf_counter() - segmentation_started) * 1000.0
            source_timing.add("segmentation", segmentation_ms)
            aggregate_timings.add("segmentation", segmentation_ms)
            jobs = [
                ProcessGameJob(
                    source_game_index=segment.source_game_index,
                    raw_text=segment.raw_text,
                    raw_headers=segment.raw_headers,
                )
                for segment in segments
            ]
            pool_run = pool.run(jobs, self.game_timeout_sec)
            self.last_pool_stats = pool_run.stats
            source_replacements = pool_run.stats.replacements - replacements_before
            results = list(pool_run.results)
            queue_waits = [result.queue_wait_ms for result in results]
            parse_times = [result.parse_ms for result in results if result.parse_count]
            identity_times = [
                result.identity_ms for result in results if result.parse_count
            ]
            if queue_waits:
                source_timing.add("queue_start_wait", sum(queue_waits), len(queue_waits))
                aggregate_timings.add("queue_start_wait", sum(queue_waits), len(queue_waits))
            if parse_times:
                source_timing.add("parse", sum(parse_times), len(parse_times))
                aggregate_timings.add("parse", sum(parse_times), len(parse_times))
            if identity_times:
                source_timing.add("identity_replay", sum(identity_times), len(identity_times))
                aggregate_timings.add("identity_replay", sum(identity_times), len(identity_times))

            def record_writer_timing(stage: str, elapsed_ms: float) -> None:
                source_timing.add(stage, elapsed_ms)
                aggregate_timings.add(stage, elapsed_ms)

            canonical_path = (
                self.workspace
                / "canonical"
                / str(tournament_id)
                / f"{raw_sha256}.pgn"
            )
            try:
                canonical_result = finalize_processed_games(
                    self.registry,
                    tournament_id,
                    source_file_id,
                    str(source_file["object_key"]),
                    results,
                    canonical_path,
                    timing_callback=record_writer_timing,
                )
            except RuntimeError as exc:
                if str(exc) != "canonicalization produced zero valid games":
                    raise
                invalid_count = sum(1 for result in results if not result.is_valid)
                total_ms = (time.perf_counter() - total_started) * 1000.0
                source_timing.add("total_source", total_ms)
                aggregate_timings.add("total_source", total_ms)
                return SourceProcessResult(
                    tournament_id=tournament_id,
                    source_file_id=source_file_id,
                    raw_sha256=raw_sha256,
                    action="review_required",
                    status=self.registry.get_tournament_status(tournament_id),
                    source_game_count=len(results),
                    valid_game_count=0,
                    invalid_game_count=invalid_count,
                    timed_out_game_count=pool_run.timed_out_games,
                    duplicate_occurrence_count=0,
                    metadata_conflict_count=0,
                    timing=source_timing.to_dict(),
                    worker_replacements=source_replacements,
                )

            self.registry.set_tournament_status(tournament_id, "CANONICALIZED")
            total_ms = (time.perf_counter() - total_started) * 1000.0
            source_timing.add("total_source", total_ms)
            aggregate_timings.add("total_source", total_ms)
            return SourceProcessResult(
                tournament_id=tournament_id,
                source_file_id=source_file_id,
                raw_sha256=raw_sha256,
                action="canonicalized",
                status=self.registry.get_tournament_status(tournament_id),
                source_game_count=canonical_result.source_game_count,
                valid_game_count=canonical_result.valid_game_count,
                invalid_game_count=canonical_result.invalid_game_count,
                timed_out_game_count=pool_run.timed_out_games,
                canonical_game_count=canonical_result.game_count,
                canonical_ply_count=canonical_result.ply_count,
                duplicate_occurrence_count=canonical_result.duplicate_occurrence_count,
                metadata_conflict_count=canonical_result.metadata_conflict_count,
                revision_id=canonical_result.revision_id,
                revision_number=canonical_result.revision_number,
                canonical_sha256=canonical_result.canonical_sha256,
                canonical_path=canonical_result.canonical_path,
                timing=source_timing.to_dict(),
                worker_replacements=source_replacements,
            )
        except Exception as exc:
            error_type, error_message = _error_fields(exc)
            total_ms = (time.perf_counter() - total_started) * 1000.0
            source_timing.add("total_source", total_ms)
            aggregate_timings.add("total_source", total_ms)
            return SourceProcessResult(
                tournament_id=tournament_id,
                source_file_id=source_file_id,
                raw_sha256=raw_sha256,
                action="failed_source",
                status=self.registry.get_tournament_status(tournament_id),
                error_type=error_type,
                error_message=error_message,
                timing=source_timing.to_dict(),
                worker_replacements=pool.replacements - replacements_before,
            )
        finally:
            try:
                raw_path.unlink()
            except FileNotFoundError:
                pass

    def process(self) -> ProcessReport:
        """Process the initial DOWNLOADED queue once, then return its report."""
        selected = select_downloaded_tournaments(self.registry)
        aggregate_timings = _TimingBook()
        results: list[SourceProcessResult] = []
        pool_stats = PoolStats()
        if not selected:
            self.last_pool_stats = pool_stats
            return ProcessReport(
                ok=True,
                selected_tournaments=0,
                processed_tournaments=0,
                canonicalized=0,
                review_required_sources=0,
                failed_sources=0,
                source_games=0,
                valid_games=0,
                invalid_games=0,
                timed_out_games=0,
                duplicate_occurrences=0,
                metadata_conflicts=0,
                workers=self.workers,
                game_timeout_sec=self.game_timeout_sec,
                timings=aggregate_timings.to_dict(),
                results=(),
            )
        self.workspace.mkdir(parents=True, exist_ok=True)
        restore_root = self.workspace / "restore"
        pool = _PersistentWorkerPool(self.workers, self.worker_operation)
        try:
            for tournament_id in selected:
                try:
                    result = self._process_tournament(
                        tournament_id,
                        restore_root,
                        pool,
                        aggregate_timings,
                    )
                except Exception as exc:
                    error_type, error_message = _error_fields(exc)
                    result = SourceProcessResult(
                        tournament_id=tournament_id,
                        source_file_id=None,
                        raw_sha256=None,
                        action="failed_source",
                        status=self.registry.get_tournament_status(tournament_id),
                        error_type=error_type,
                        error_message=error_message,
                        timing=_empty_timings(),
                    )
                results.append(result)
            pool_stats = pool.stats
        finally:
            pool._shutdown()
            self.last_pool_stats = pool_stats

        source_games = sum(result.source_game_count for result in results)
        valid_games = sum(result.valid_game_count for result in results)
        invalid_games = sum(result.invalid_game_count for result in results)
        timed_out_games = sum(result.timed_out_game_count for result in results)
        return ProcessReport(
            ok=not any(result.action == "failed_source" for result in results),
            selected_tournaments=len(selected),
            processed_tournaments=len(results),
            canonicalized=sum(result.action == "canonicalized" for result in results),
            review_required_sources=sum(
                result.action == "review_required" for result in results
            ),
            failed_sources=sum(result.action == "failed_source" for result in results),
            source_games=source_games,
            valid_games=valid_games,
            invalid_games=invalid_games,
            timed_out_games=timed_out_games,
            duplicate_occurrences=sum(
                result.duplicate_occurrence_count for result in results
            ),
            metadata_conflicts=sum(
                result.metadata_conflict_count for result in results
            ),
            workers=self.workers,
            game_timeout_sec=self.game_timeout_sec,
            timings=aggregate_timings.to_dict(),
            results=tuple(results),
            worker_replacements=sum(result.worker_replacements for result in results),
        )


__all__ = [
    "CorpusProcessor",
    "PoolStats",
    "ProcessGameJob",
    "ProcessReport",
    "SourceGameSlice",
    "SourceProcessResult",
    "delayed_worker_operation",
    "process_game_job",
    "resolve_worker_count",
    "segment_pgn_bytes",
    "segment_pgn_text",
    "select_downloaded_tournaments",
    "select_latest_source_file",
]
