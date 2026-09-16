from __future__ import annotations

import json

from chessgrandmaster.discovery.chess_results import (
    ChessResultsCandidate,
    CorpusDiscoveryResult,
    CorpusWindow,
)


def _candidate(external_id: str, country: str) -> ChessResultsCandidate:
    return ChessResultsCandidate(
        provider="chess-results",
        external_id=external_id,
        source_url=f"https://chess-results.com/{external_id}.aspx",
        title=f"Tournament {external_id}",
        time_control_hint="standard",
        evidence=("corpus:year:2026",),
        event_country=country,
        from_date="2026-01-01",
        to_date="2026-01-02",
        database_key=external_id.removeprefix("tnr"),
        provider_game_count=10,
    )


def test_corpus_cli_scans_full_set_before_limit_and_writes_deterministic_report(
    monkeypatch,
    capsys,
    tmp_path,
):
    from chessgrandmaster.discovery import cli

    candidates = (_candidate("tnr1001", "VIE"), _candidate("tnr1002", "GER"))
    scanned = CorpusDiscoveryResult(
        candidates=candidates,
        windows=(CorpusWindow("2026-01-01", "2026-01-31", 2),),
    )
    seen: dict[str, object] = {}

    class _FakeDiscovery:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def discover_corpus(self, **kwargs):
            seen["scan"] = kwargs
            return scanned

        def enrich_corpus_priority(self, result, *, year, limit=None):
            seen["enrich"] = (result, year, limit)
            return CorpusDiscoveryResult(
                candidates=(result.candidates[0],),
                windows=result.windows,
                errors=result.errors,
            )

    class _UnexpectedB2:
        def __init__(self, *args, **kwargs):
            raise AssertionError("B2 must not be constructed without --acquire")

    monkeypatch.setattr(cli, "ChessResultsDiscovery", _FakeDiscovery)
    monkeypatch.setattr(cli, "Registry", _UnexpectedB2, raising=False)
    monkeypatch.setattr(cli, "LocalObjectStore", _UnexpectedB2, raising=False)
    monkeypatch.setattr(cli, "AcquisitionService", _UnexpectedB2, raising=False)
    monkeypatch.setattr(cli, "DiscoveryAcquisitionService", _UnexpectedB2, raising=False)

    report_path = tmp_path / "reports" / "corpus.json"
    assert cli.main(
        [
            "chess-results",
            "corpus",
            "--year",
            "2026",
            "--max-lines",
            "2000",
            "--limit",
            "1",
            "--report",
            str(report_path),
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["mode"] == "corpus"
    assert payload["candidate_count"] == 1
    assert payload["complete"] is True
    assert seen["scan"] == {
        "year": 2026,
        "max_lines": 2000,
        "limit": None,
        "country": None,
    }
    assert seen["enrich"][1:] == (2026, 1)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["year"] == 2026
    assert report["max_lines"] == 2000
    assert report["only_finished"] is True
    assert report["games_available"] is True
    assert report["complete"] is True
    assert report["candidate_count"] == 1
    assert report["priority_counts"] == {
        "book_high": 0,
        "book_medium": 1,
        "general": 0,
    }


def test_downloadable_diagnostic_accepts_country_without_changing_corpus_default(
    monkeypatch,
    capsys,
):
    from chessgrandmaster.discovery import cli

    candidate = _candidate("tnr2001", "VIE")
    scanned = CorpusDiscoveryResult(
        candidates=(candidate,),
        windows=(CorpusWindow("2026-01-01", "2026-01-31", 1),),
    )
    seen = {}

    class _FakeDiscovery:
        def __init__(self, **_kwargs):
            pass

        def discover_corpus(self, **kwargs):
            seen.update(kwargs)
            return scanned

    monkeypatch.setattr(cli, "ChessResultsDiscovery", _FakeDiscovery)
    assert cli.main(
        [
            "chess-results",
            "downloadable",
            "--year",
            "2026",
            "--country",
            "VIE",
            "--limit",
            "1",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "downloadable"
    assert payload["candidate_count"] == 1
    assert seen == {
        "year": 2026,
        "max_lines": 2000,
        "limit": None,
        "country": "VIE",
    }


def test_corpus_acquire_routes_every_post_priority_candidate_to_existing_bridge(
    monkeypatch,
    capsys,
    tmp_path,
):
    from chessgrandmaster.discovery import cli
    from chessgrandmaster.discovery.acquire import (
        CandidateAcquisition,
        DiscoveryAcquisitionResult,
    )

    candidates = (_candidate("tnr3001", "THA"), _candidate("tnr3002", "GER"))
    scanned = CorpusDiscoveryResult(
        candidates=candidates,
        windows=(CorpusWindow("2026-01-01", "2026-01-31", 2),),
    )
    seen = {}

    class _FakeDiscovery:
        def __init__(self, **_kwargs):
            pass

        def discover_corpus(self, **kwargs):
            seen["scan"] = kwargs
            return scanned

        def enrich_corpus_priority(self, result, *, year, limit=None):
            seen["enrich"] = (year, limit)
            return result

    class _FakeBridge:
        def acquire_candidates(self, received):
            received = tuple(received)
            seen["received"] = received
            return DiscoveryAcquisitionResult(
                discovered=len(received),
                attempted=len(received),
                acquired=len(received),
                skipped_existing=0,
                skipped_no_pgn=0,
                failed=0,
                results=tuple(
                    CandidateAcquisition(
                        provider=item.provider,
                        external_id=item.external_id,
                        source_url=item.source_url,
                        action="acquired",
                        tournament_id=index,
                        tournament_status="CANONICALIZED",
                        raw_sha256="a" * 64,
                        revision_id=index,
                    )
                    for index, item in enumerate(received, 1)
                ),
            )

    monkeypatch.setattr(cli, "ChessResultsDiscovery", _FakeDiscovery)
    monkeypatch.setattr(cli, "_build_acquisition_service", lambda *_args: _FakeBridge())

    assert cli.main(
        [
            "chess-results",
            "corpus",
            "--year",
            "2026",
            "--acquire",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "b2"),
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["acquisition"]["acquired"] == 2
    assert [item.external_id for item in seen["received"]] == [
        "tnr3001",
        "tnr3002",
    ]
    assert seen["scan"]["limit"] is None
    assert seen["enrich"] == (2026, None)


def test_corpus_acquire_fails_closed_before_constructing_b2_when_scan_is_incomplete(
    monkeypatch,
    capsys,
    tmp_path,
):
    from chessgrandmaster.discovery import cli

    candidate = _candidate("tnr4001", "GER")
    incomplete = CorpusDiscoveryResult(
        candidates=(candidate,),
        windows=(
            CorpusWindow(
                "2026-01-01",
                "2026-01-01",
                2000,
                saturated=True,
            ),
        ),
        errors=("source_window:2026-02-01:2026-02-28:RuntimeError:HTTP error",),
    )
    seen = {"enriched": False, "built": False}

    class _FakeDiscovery:
        def __init__(self, **_kwargs):
            pass

        def discover_corpus(self, **kwargs):
            return incomplete

        def enrich_corpus_priority(self, *args, **kwargs):
            seen["enriched"] = True
            raise AssertionError("incomplete corpus must not be enriched")

    def _unexpected_build(*_args):
        seen["built"] = True
        raise AssertionError("B2 must not be constructed for incomplete corpus")

    monkeypatch.setattr(cli, "ChessResultsDiscovery", _FakeDiscovery)
    monkeypatch.setattr(cli, "_build_acquisition_service", _unexpected_build)

    report_path = tmp_path / "reports" / "incomplete.json"
    exit_code = cli.main(
        [
            "chess-results",
            "corpus",
            "--acquire",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "b2"),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "discovery": {
            "candidate_count": 1,
            "errors": [
                "source_window:2026-02-01:2026-02-28:RuntimeError:HTTP error"
            ],
            "saturated_windows": [
                {
                    "from_date": "2026-01-01",
                    "parsed_candidate_count": 0,
                    "returned_row_count": 2000,
                    "saturated": True,
                    "source_window_saturated": True,
                    "split": False,
                    "to_date": "2026-01-01",
                }
            ],
        },
        "error": {
            "message": "corpus discovery is incomplete",
            "type": "CorpusIncompleteError",
        },
        "mode": "corpus",
        "ok": False,
        "provider": "chess-results",
    }
    assert json.loads(report_path.read_text(encoding="utf-8"))["complete"] is False
    assert seen == {"enriched": False, "built": False}
