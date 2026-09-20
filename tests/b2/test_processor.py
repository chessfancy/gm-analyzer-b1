from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import time

import pytest

import chessgrandmaster.b2.processor as processor_module
from chessgrandmaster.b2.canonicalize import canonicalize_source_file
from chessgrandmaster.b2.cli_process import build_parser, main
from chessgrandmaster.b2.processor import (
    CorpusProcessor,
    ProcessGameJob,
    PoolStats,
    _PoolRun,
    _TimingBook,
    process_game_job,
    resolve_worker_count,
    segment_pgn_text,
    select_downloaded_tournaments,
    select_latest_source_file,
)
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.b2.storage import LocalObjectStore


FIXTURES = Path(__file__).parent / "fixtures"


SINGLE_GAME = (
    '[Event "Single"]\n'
    '[Site "Hanoi"]\n'
    '[Date "2026.09.17"]\n'
    '[Round "1"]\n'
    '[White "White"]\n'
    '[Black "Black"]\n'
    '[Result "1-0"]\n'
    '\n'
    '1. e4 e5 2. Nf3 Nc6 1-0\n'
).encode("utf-8")


INVALID_GAME = (
    '[Event "Invalid"]\n'
    '[Result "*"]\n'
    '\n'
    '1. e4 e5 2. e4 *\n'
).encode("utf-8")


def _seed_source(
    tmp_path: Path,
    registry: Registry,
    store: LocalObjectStore,
    tournament_id: int,
    raw_bytes: bytes,
    label: str,
    *,
    downloaded_at: str | None = None,
) -> tuple[int, int]:
    raw_path = tmp_path / f"{label}.pgn"
    raw_path.write_bytes(raw_bytes)
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    source_id = registry.upsert_source(
        f"fixture-{label}",
        "https://example.invalid",
        priority=10,
    )
    source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id=label,
        source_url=f"https://example.invalid/{label}",
    )
    object_key = f"tournaments/{tournament_id}/source/fixture/{sha256}/original.pgn"
    store.put_file(object_key, raw_path)
    source_file_id = registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key=object_key,
        filename=raw_path.name,
        sha256=sha256,
        byte_size=len(raw_bytes),
        content_type="application/x-chess-pgn",
        downloaded_at=downloaded_at,
    )
    return source_file_id, tournament_id


def _processor(tmp_path: Path, registry: Registry, *, workers=1, timeout=2.0, operation=None):
    store = LocalObjectStore(tmp_path / "objects")
    return CorpusProcessor(
        registry=registry,
        store=store,
        workspace=tmp_path / "workspace",
        workers=workers,
        game_timeout_sec=timeout,
        worker_operation=operation,
    ), store


def _sleep_first_operation(job: ProcessGameJob):
    if job.source_game_index == 1:
        time.sleep(0.25)
    return process_game_job(job)


def test_segmenter_preserves_games_and_ignores_event_text_in_comments():
    text = (
        '[Event "one"]\n[Result "*"]\n\n'
        '1. e4 { comment starts\n'
        '[Event "not-a-boundary"]\n'
        '} e5 *\n\n'
        '[Event "two"]\n[Result "*"]\n\n1. d4 d5 *\n'
    )

    segments = segment_pgn_text(text)

    assert [segment.source_game_index for segment in segments] == [1, 2]
    assert len(segments) == 2
    assert segments[0].raw_headers["Event"] == "one"
    assert segments[1].raw_headers["Event"] == "two"
    assert '[Event "not-a-boundary"]' in segments[0].raw_text


def test_segmenter_handles_malformed_boundaries_without_naive_event_split():
    text = (
        'preamble without a tag\n'
        '[Event "one"]\n[Result "*"]\n\n1. e4 *\n'
        '[Event "two"]\n[Result "*"]\n\n1. d4 *\n'
    )

    segments = segment_pgn_text(text)

    assert len(segments) == 2
    assert segments[0].source_game_index == 1
    assert segments[1].source_game_index == 2
    assert segments[0].raw_headers["Event"] == "one"


def test_queue_selects_only_exact_downloaded_status_and_latest_file_is_deterministic(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    downloaded = registry.upsert_tournament("downloaded", status="DOWNLOADED")
    canonicalized = registry.upsert_tournament("canonicalized", status="CANONICALIZED")
    sharded = registry.upsert_tournament("sharded", status="SHARDED")
    ready = registry.upsert_tournament("ready", status="READY")
    assert select_downloaded_tournaments(registry) == [downloaded]

    store = LocalObjectStore(tmp_path / "objects")
    first_id, _ = _seed_source(
        tmp_path,
        registry,
        store,
        downloaded,
        SINGLE_GAME,
        "first",
        downloaded_at="2026-09-17T00:00:00Z",
    )
    second_id, _ = _seed_source(
        tmp_path,
        registry,
        store,
        downloaded,
        INVALID_GAME,
        "second",
        downloaded_at="2026-09-17T00:00:00Z",
    )
    selected = select_latest_source_file(registry, downloaded)
    assert selected["id"] == second_id
    assert selected["id"] != first_id
    assert registry.get_tournament_status(canonicalized) == "CANONICALIZED"
    assert registry.get_tournament_status(sharded) == "SHARDED"
    assert registry.get_tournament_status(ready) == "READY"


def test_process_game_job_parses_one_source_game_once_and_preserves_identity():
    segment = segment_pgn_text(SINGLE_GAME.decode("utf-8"))[0]

    result = process_game_job(
        ProcessGameJob(
            source_game_index=segment.source_game_index,
            raw_text=segment.raw_text,
            raw_headers=segment.raw_headers,
        )
    )

    assert result.is_valid is True
    assert result.identity is not None
    assert result.identity.ply_count == 4
    assert result.parse_count == 1
    assert result.parse_error is None


def test_mixed_source_keeps_valid_games_and_records_invalid_occurrence(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("mixed", status="DOWNLOADED")
    raw = SINGLE_GAME + b"\n" + INVALID_GAME + b"\n" + SINGLE_GAME
    _seed_source(tmp_path, registry, LocalObjectStore(tmp_path / "objects"), tournament_id, raw, "mixed")
    processor, _ = _processor(tmp_path, registry)

    report = processor.process()

    assert report.canonicalized == 1
    assert report.invalid_games == 1
    assert report.timed_out_games == 0
    assert report.results[0].source_game_count == 3
    assert report.results[0].valid_game_count == 2
    assert report.results[0].invalid_game_count == 1
    assert registry.get_tournament_status(tournament_id) == "CANONICALIZED"
    assert registry.registry_counts()["game_occurrences"] == 3
    with registry._connect() as connection:
        invalid_rows = connection.execute(
            "SELECT * FROM game_occurrences WHERE is_valid = 0"
        ).fetchall()
    assert len(invalid_rows) == 1
    assert invalid_rows[0]["canonical_game_id"] is None
    assert invalid_rows[0]["source_game_index"] == 2
    assert invalid_rows[0]["parse_error"]


def test_zero_valid_source_is_review_required_and_does_not_block_next_source(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    first = registry.upsert_tournament("all-invalid", status="DOWNLOADED")
    second = registry.upsert_tournament("valid-next", status="DOWNLOADED")
    store = LocalObjectStore(tmp_path / "objects")
    _seed_source(tmp_path, registry, store, first, INVALID_GAME, "invalid")
    _seed_source(tmp_path, registry, store, second, SINGLE_GAME, "valid")
    processor, _ = _processor(tmp_path, registry)

    report = processor.process()

    assert report.selected_tournaments == 2
    assert report.processed_tournaments == 2
    assert report.review_required_sources == 1
    assert report.canonicalized == 1
    assert [result.action for result in report.results] == [
        "review_required",
        "canonicalized",
    ]
    assert registry.get_tournament_status(first) == "DOWNLOADED"
    assert registry.get_tournament_status(second) == "CANONICALIZED"
    assert registry.registry_counts()["tournament_revisions"] == 1
    assert registry.registry_counts()["game_occurrences"] == 2


def test_timeout_terminates_owner_replaces_worker_and_continues(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("timeout", status="DOWNLOADED")
    raw = SINGLE_GAME + b"\n" + SINGLE_GAME + b"\n" + SINGLE_GAME
    _seed_source(tmp_path, registry, LocalObjectStore(tmp_path / "objects"), tournament_id, raw, "timeout")
    processor, _ = _processor(
        tmp_path,
        registry,
        workers=2,
        timeout=0.05,
        operation=_sleep_first_operation,
    )

    report = processor.process()

    assert report.canonicalized == 1
    assert report.timed_out_games == 1
    assert report.results[0].timed_out_game_count == 1
    assert report.results[0].valid_game_count == 2
    assert report.worker_replacements >= 1
    assert processor.last_pool_stats.replacements >= 1
    assert processor.last_pool_stats.terminated_worker_pids
    assert all(not alive for alive in processor.last_pool_stats.terminated_worker_alive)
    with registry._connect() as connection:
        timeout_rows = connection.execute(
            "SELECT * FROM game_occurrences WHERE parse_error LIKE 'ProcessingTimeout:%'"
        ).fetchall()
    assert len(timeout_rows) == 1
    assert timeout_rows[0]["canonical_game_id"] is None
    assert timeout_rows[0]["is_valid"] == 0
    assert timeout_rows[0]["source_game_index"] == 1


def test_processed_projection_matches_existing_canonicalizer(tmp_path):
    raw = (FIXTURES / "duplicate_conflict_a.pgn").read_bytes()

    old_root = tmp_path / "old"
    old_registry = Registry(old_root / "registry.sqlite")
    old_tournament = old_registry.upsert_tournament("fixture", status="DOWNLOADED")
    old_store = LocalObjectStore(old_root / "objects")
    old_source_file, _ = _seed_source(
        old_root,
        old_registry,
        old_store,
        old_tournament,
        raw,
        "fixture",
    )
    old_raw_path = old_root / "fixture.pgn"
    old_result = canonicalize_source_file(
        old_registry,
        old_tournament,
        old_source_file,
        old_raw_path,
        old_root / "canonical.pgn",
    )

    new_root = tmp_path / "new"
    new_registry = Registry(new_root / "registry.sqlite")
    new_tournament = new_registry.upsert_tournament("fixture", status="DOWNLOADED")
    new_store = LocalObjectStore(new_root / "objects")
    _seed_source(new_root, new_registry, new_store, new_tournament, raw, "fixture")
    new_processor = CorpusProcessor(
        registry=new_registry,
        store=new_store,
        workspace=new_root / "workspace",
        workers=1,
    )
    new_report = new_processor.process()
    new_result = new_report.results[0]

    assert new_result.canonical_sha256 == old_result.canonical_sha256
    assert new_result.canonical_game_count == old_result.game_count
    assert new_result.canonical_ply_count == old_result.ply_count
    assert Path(new_result.canonical_path).read_bytes() == (old_root / "canonical.pgn").read_bytes()
    assert new_registry.registry_counts()["game_occurrences"] == old_registry.registry_counts()["game_occurrences"]


def test_processor_rerun_skips_canonicalized_without_duplicates(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("rerun", status="DOWNLOADED")
    _seed_source(tmp_path, registry, LocalObjectStore(tmp_path / "objects"), tournament_id, SINGLE_GAME, "rerun")
    processor, _ = _processor(tmp_path, registry)

    first = processor.process()
    counts_after_first = registry.registry_counts()
    second = processor.process()

    assert first.canonicalized == 1
    assert second.selected_tournaments == 0
    assert second.processed_tournaments == 0
    assert registry.registry_counts() == counts_after_first


def test_interrupted_downloaded_tournament_resumes_without_duplicate_rows(
    tmp_path, monkeypatch
):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("interrupted", status="DOWNLOADED")
    raw = SINGLE_GAME + b"\n" + INVALID_GAME + b"\n" + SINGLE_GAME
    _seed_source(
        tmp_path,
        registry,
        LocalObjectStore(tmp_path / "objects"),
        tournament_id,
        raw,
        "interrupted",
    )
    processor, _ = _processor(tmp_path, registry)
    real_finalize = processor_module.finalize_processed_games

    def interrupt_after_persist(*args, **kwargs):
        result = real_finalize(*args, **kwargs)
        raise KeyboardInterrupt("simulated operator stop")

    monkeypatch.setattr(
        processor_module,
        "finalize_processed_games",
        interrupt_after_persist,
    )

    with pytest.raises(KeyboardInterrupt, match="simulated operator stop"):
        processor.process()

    assert registry.get_tournament_status(tournament_id) == "DOWNLOADED"
    interrupted_counts = registry.registry_counts()
    assert interrupted_counts["canonical_games"] == 1
    assert interrupted_counts["game_occurrences"] == 3
    assert interrupted_counts["tournament_revisions"] == 1
    assert interrupted_counts["tournament_games"] == 1
    interrupted_revision = registry.list_revisions_for_tournament(tournament_id)
    assert len(interrupted_revision) == 1
    interrupted_revision_id = interrupted_revision[0]["revision_id"]
    interrupted_membership = registry.get_revision_games(interrupted_revision_id)
    canonical_game_id = interrupted_membership[0]["canonical_game_id"]
    interrupted_occurrences = registry.get_occurrences(canonical_game_id)
    assert len(interrupted_occurrences) == 2
    with registry._connect() as connection:
        invalid_rows = connection.execute(
            """
            SELECT id, canonical_game_id, is_valid, parse_error, source_game_index
            FROM game_occurrences
            WHERE source_file_id = (SELECT id FROM source_files WHERE tournament_id = ?)
              AND is_valid = 0
            """,
            (tournament_id,),
        ).fetchall()
    assert len(invalid_rows) == 1
    assert invalid_rows[0]["canonical_game_id"] is None
    assert invalid_rows[0]["parse_error"]
    invalid_occurrence_id = invalid_rows[0]["id"]

    monkeypatch.setattr(processor_module, "finalize_processed_games", real_finalize)
    resumed = processor.process()

    assert resumed.selected_tournaments == 1
    assert resumed.canonicalized == 1
    assert resumed.review_required_sources == 0
    assert resumed.results[0].valid_game_count == 2
    assert resumed.results[0].invalid_game_count == 1
    assert resumed.results[0].duplicate_occurrence_count == 1
    assert resumed.source_games == 3
    assert resumed.valid_games == 2
    assert resumed.invalid_games == 1
    assert resumed.timed_out_games == 0
    assert resumed.duplicate_occurrences == 1
    assert resumed.metadata_conflicts == 0
    assert resumed.worker_replacements == 0
    assert resumed.results[0].timing["restore_checksum"]["count"] == 1
    assert resumed.results[0].timing["segmentation"]["count"] == 1
    for stage in ("queue_start_wait", "parse", "identity_replay"):
        assert resumed.results[0].timing[stage]["count"] == 3
    for stage in (
        "sqlite_write",
        "metadata_display_selection",
        "projection_export",
        "revision_finalization",
        "total_source",
    ):
        assert resumed.results[0].timing[stage]["count"] == 1
    assert resumed.timings == resumed.results[0].timing
    assert resumed.to_dict()["results"][0]["action"] == "canonicalized"
    assert resumed.to_dict()["timings"] == resumed.timings
    assert registry.get_tournament_status(tournament_id) == "CANONICALIZED"
    assert registry.registry_counts() == interrupted_counts
    resumed_revision = registry.list_revisions_for_tournament(tournament_id)
    assert resumed_revision == interrupted_revision
    assert registry.get_occurrences(canonical_game_id) == interrupted_occurrences
    with registry._connect() as connection:
        resumed_invalid = connection.execute(
            """
            SELECT id, canonical_game_id, is_valid, parse_error, source_game_index
            FROM game_occurrences WHERE id = ?
            """,
            (invalid_occurrence_id,),
        ).fetchone()
    assert resumed_invalid["id"] == invalid_occurrence_id
    assert resumed_invalid["canonical_game_id"] is None
    assert resumed_invalid["is_valid"] == 0
    assert resumed_invalid["parse_error"]


def test_preexisting_invalid_occurrence_is_upgraded_without_duplicate_row(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("resume", status="DOWNLOADED")
    store = LocalObjectStore(tmp_path / "objects")
    source_file_id, _ = _seed_source(
        tmp_path,
        registry,
        store,
        tournament_id,
        SINGLE_GAME,
        "resume",
    )
    source_file = registry.get_source_file(source_file_id)
    registry.record_occurrence(
        canonical_game_id=None,
        tournament_id=tournament_id,
        source_file_id=source_file_id,
        source_game_index=1,
        raw_headers={"Event": "old"},
        raw_pgn_object_key=source_file["object_key"],
        is_valid=False,
        parse_error="old invalid result",
    )
    processor, _ = _processor(tmp_path, registry)

    report = processor.process()

    assert report.canonicalized == 1
    assert registry.registry_counts()["game_occurrences"] == 1
    with registry._connect() as connection:
        row = connection.execute(
            "SELECT canonical_game_id, is_valid, parse_error FROM game_occurrences"
        ).fetchone()
    assert row["canonical_game_id"] is not None
    assert row["is_valid"] == 1
    assert row["parse_error"] is None


def test_worker_policy_and_cli_report_contract(tmp_path, capsys):
    assert resolve_worker_count("auto") == max(1, min(8, (os.cpu_count() or 2) - 1))
    assert resolve_worker_count("2") == 2
    assert build_parser().parse_args(
        ["--registry", "registry.sqlite", "--root", "root", "--workers", "auto"]
    ).workers == "auto"

    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("cli", status="DOWNLOADED")
    _seed_source(tmp_path, registry, LocalObjectStore(tmp_path / "objects"), tournament_id, SINGLE_GAME, "cli")
    report_path = tmp_path / "report.json"

    exit_code = main(
        [
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path),
            "--workers",
            "1",
            "--game-timeout-sec",
            "0.5",
            "--report",
            str(report_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["selected_tournaments"] == 1
    assert payload["workers"] == 1
    assert payload["game_timeout_sec"] == 0.5
    assert payload["results"][0]["action"] == "canonicalized"
    assert json.loads(report_path.read_text(encoding="utf-8")) == payload
    assert payload["timings"]["segmentation"]["count"] == 1
    assert payload["timings"]["parse"]["count"] == 1


def test_timing_book_add_samples_uses_sample_max_without_changing_single_add():
    timing = _TimingBook()

    timing.add_samples("parse", [4.0, 5.0, 3.0])
    timing.add("segmentation", 7.0)

    assert timing.to_dict()["parse"] == {
        "count": 3,
        "total_ms": 12.0,
        "max_ms": 5.0,
    }
    assert timing.to_dict()["segmentation"] == {
        "count": 1,
        "total_ms": 7.0,
        "max_ms": 7.0,
    }


def test_processor_report_uses_sample_max_for_per_game_timing_batches(
    tmp_path, monkeypatch
):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament("timing", status="DOWNLOADED")
    _seed_source(
        tmp_path,
        registry,
        LocalObjectStore(tmp_path / "objects"),
        tournament_id,
        SINGLE_GAME + b"\n" + SINGLE_GAME + b"\n" + SINGLE_GAME,
        "timing",
    )

    class _DeterministicPool:
        replacements = 0

        def __init__(self, workers, operation):
            self._stats = PoolStats()

        @property
        def stats(self):
            return self._stats

        def run(self, jobs, timeout_sec):
            results = []
            for job, sample in zip(jobs, (4.0, 5.0, 3.0)):
                result = process_game_job(job)
                results.append(
                    replace(
                        result,
                        queue_wait_ms=sample,
                        parse_ms=sample,
                        identity_ms=sample,
                    )
                )
            return _PoolRun(tuple(results), 0, self._stats)

        def _shutdown(self):
            return None

    monkeypatch.setattr(
        processor_module,
        "_PersistentWorkerPool",
        _DeterministicPool,
    )
    processor, _ = _processor(tmp_path, registry)

    report = processor.process()
    source_timing = report.results[0].timing

    for stage in ("queue_start_wait", "parse", "identity_replay"):
        expected = {"count": 3, "total_ms": 12.0, "max_ms": 5.0}
        assert source_timing[stage] == expected
        assert report.timings[stage] == expected
