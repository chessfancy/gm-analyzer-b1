from __future__ import annotations

import json

from chessgrandmaster.discovery.acquire import (
    CandidateAcquisition,
    DiscoveryAcquisitionResult,
)
from chessgrandmaster.discovery.chess_results import (
    ChessResultsCandidate,
    CorpusDiscoveryResult,
    CorpusWindow,
)


def _candidate(external_id: str = "tnr1450909") -> ChessResultsCandidate:
    return ChessResultsCandidate(
        provider="chess-results",
        external_id=external_id,
        source_url=f"https://chess-results.com/{external_id}.aspx",
        title="Fixture tournament",
        time_control_hint="standard",
        evidence=("fixture",),
        event_country="VIE",
        from_date="2026-01-01",
        to_date="2026-01-02",
    )


def _fetch_batch(candidate: ChessResultsCandidate) -> DiscoveryAcquisitionResult:
    return DiscoveryAcquisitionResult(
        discovered=1,
        attempted=1,
        acquired=1,
        skipped_existing=0,
        skipped_no_pgn=0,
        failed=0,
        results=(
            CandidateAcquisition(
                provider=candidate.provider,
                external_id=candidate.external_id,
                source_url=candidate.source_url,
                action="fetched",
                tournament_id=17,
                tournament_status="DOWNLOADED",
                raw_sha256="a" * 64,
                revision_id=None,
                source_file_id=31,
            ),
        ),
    )


def test_fetch_cli_emits_fetch_report_and_writes_audit_report(
    monkeypatch,
    capsys,
    tmp_path,
):
    from chessgrandmaster.b2 import cli_fetch

    candidate = _candidate()
    scanned = CorpusDiscoveryResult(
        candidates=(candidate,),
        windows=(CorpusWindow("2026-01-01", "2026-01-31", 1),),
    )
    report_path = tmp_path / "reports" / "fetch.json"
    seen: dict[str, object] = {}

    class _Discovery:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def discover_corpus(self, **kwargs):
            seen["scan"] = kwargs
            return scanned

        def enrich_corpus_priority(self, result, *, year, limit=None):
            seen["enrich"] = (year, limit)
            return result

    class _Bridge:
        def fetch_candidates(self, candidates, *, refresh_recent_days):
            seen["candidates"] = tuple(candidates)
            seen["refresh_recent_days"] = refresh_recent_days
            return _fetch_batch(candidate)

    monkeypatch.setattr(cli_fetch, "ChessResultsDiscovery", _Discovery)
    monkeypatch.setattr(cli_fetch, "_build_fetch_service", lambda *_args: _Bridge())

    exit_code = cli_fetch.main(
        [
            "chess-results",
            "corpus",
            "--year",
            "2026",
            "--max-lines",
            "2000",
            "--refresh-recent-days",
            "0",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "root"),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["candidate_count"] == 1
    assert payload["complete"] is True
    assert payload["fetched"] == 1
    assert payload["skipped_existing"] == 0
    assert payload["skipped_no_pgn"] == 0
    assert payload["failed"] == 0
    assert payload["results"][0]["source_file_id"] == 31
    assert payload["results"][0]["raw_sha256"] == "a" * 64
    assert seen["refresh_recent_days"] == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["fetched"] == 1
    assert report["results"] == payload["results"]


def test_fetch_cli_blocks_incomplete_corpus_before_building_b2(
    monkeypatch,
    capsys,
    tmp_path,
):
    from chessgrandmaster.b2 import cli_fetch

    incomplete = CorpusDiscoveryResult(
        candidates=(_candidate(),),
        windows=(
            CorpusWindow(
                "2026-01-01",
                "2026-01-01",
                2000,
                saturated=True,
            ),
        ),
        errors=("source_window:error",),
    )

    class _Discovery:
        def __init__(self, **_kwargs):
            pass

        def discover_corpus(self, **_kwargs):
            return incomplete

        def enrich_corpus_priority(self, *_args, **_kwargs):
            raise AssertionError("priority enrichment must not run")

    def _unexpected(*_args):
        raise AssertionError("fetch service must not be built")

    monkeypatch.setattr(cli_fetch, "ChessResultsDiscovery", _Discovery)
    monkeypatch.setattr(cli_fetch, "_build_fetch_service", _unexpected)

    exit_code = cli_fetch.main(
        [
            "chess-results",
            "corpus",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "root"),
        ]
    )

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "CorpusIncompleteError"
    assert payload["complete"] is False
    assert not (tmp_path / "registry.sqlite").exists()
    assert not (tmp_path / "root").exists()


def test_fetch_cli_allows_priority_warning_and_still_fetches(
    monkeypatch,
    capsys,
    tmp_path,
):
    from chessgrandmaster.b2 import cli_fetch

    candidate = _candidate()
    scanned = CorpusDiscoveryResult(
        candidates=(candidate,),
        windows=(CorpusWindow("2026-01-01", "2026-01-31", 1),),
    )
    enriched = CorpusDiscoveryResult(
        candidates=(candidate,),
        windows=scanned.windows,
        priority_errors=("priority:provider unavailable",),
    )

    class _Discovery:
        def __init__(self, **_kwargs):
            pass

        def discover_corpus(self, **_kwargs):
            return scanned

        def enrich_corpus_priority(self, *_args, **_kwargs):
            return enriched

    class _Bridge:
        def fetch_candidates(self, *_args, **_kwargs):
            return _fetch_batch(candidate)

    monkeypatch.setattr(cli_fetch, "ChessResultsDiscovery", _Discovery)
    monkeypatch.setattr(cli_fetch, "_build_fetch_service", lambda *_args: _Bridge())

    assert cli_fetch.main(
        [
            "chess-results",
            "corpus",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "root"),
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["complete"] is True
    assert payload["priority_complete"] is False
    assert payload["priority_errors"] == ["priority:provider unavailable"]
    assert payload["fetched"] == 1