from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.discovery.chess_results import ChessResultsCandidate
from chessgrandmaster.discovery.acquire import DiscoveryAcquisitionService


SOURCE_URL = "https://chess-results.com/tnr1450909.aspx?lan=1"


def _candidate(external_id: str = "tnr1450909") -> ChessResultsCandidate:
    return ChessResultsCandidate(
        provider="chess-results",
        external_id=external_id,
        source_url=SOURCE_URL.replace("1450909", external_id.removeprefix("tnr")),
        title="Fixture tournament",
        time_control_hint="standard",
        evidence=("fixture",),
    )


class _FakeAcquisition:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls: list[str] = []

    def acquire(self, source_url: str):
        self.calls.append(source_url)
        outcome = self.outcomes[source_url]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _register_candidate(
    registry: Registry,
    candidate: ChessResultsCandidate,
    status: str,
) -> int:
    source_id = registry.upsert_source(
        candidate.provider,
        "https://chess-results.com",
    )
    tournament_id = registry.upsert_tournament(
        f"{candidate.provider}-{candidate.external_id}",
        status=status,
    )
    registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id=candidate.external_id,
        source_url=candidate.source_url,
    )
    return tournament_id


def test_new_candidate_is_acquired_and_returns_structured_result(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate()
    acquisition = _FakeAcquisition(
        {
            candidate.source_url: SimpleNamespace(
                tournament_id=17,
                tournament_status="CANONICALIZED",
                raw_sha256="a" * 64,
                revision_id=23,
            )
        }
    )
    service = DiscoveryAcquisitionService(registry, acquisition)

    batch = service.acquire_candidates([candidate])

    assert acquisition.calls == [candidate.source_url]
    assert batch.discovered == 1
    assert batch.attempted == 1
    assert batch.acquired == 1
    assert batch.skipped_existing == 0
    assert batch.failed == 0
    assert len(batch.results) == 1
    assert batch.results[0].provider == "chess-results"
    assert batch.results[0].external_id == "tnr1450909"
    assert batch.results[0].source_url == candidate.source_url
    assert batch.results[0].action == "acquired"
    assert batch.results[0].tournament_id == 17
    assert batch.results[0].tournament_status == "CANONICALIZED"
    assert batch.results[0].raw_sha256 == "a" * 64
    assert batch.results[0].revision_id == 23
    assert batch.results[0].error_type is None
    assert batch.results[0].error_message is None


@pytest.mark.parametrize("status", ["CANONICALIZED", "SHARDED", "READY"])
def test_completed_candidate_is_skipped_without_acquisition(tmp_path, status):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate()
    tournament_id = _register_candidate(registry, candidate, status)
    before = registry.registry_counts()
    acquisition = _FakeAcquisition({})

    batch = DiscoveryAcquisitionService(registry, acquisition).acquire_candidates(
        [candidate]
    )

    assert acquisition.calls == []
    assert batch.discovered == batch.attempted == 1
    assert batch.acquired == batch.failed == 0
    assert batch.skipped_existing == 1
    assert batch.results[0].action == "skipped_existing"
    assert batch.results[0].tournament_id == tournament_id
    assert batch.results[0].tournament_status == status
    assert registry.registry_counts() == before


@pytest.mark.parametrize("status", ["DISCOVERED", "DOWNLOADED", "VALIDATED"])
def test_incomplete_candidate_is_retried_through_acquisition(tmp_path, status):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate()
    _register_candidate(registry, candidate, status)
    acquisition = _FakeAcquisition(
        {
            candidate.source_url: SimpleNamespace(
                tournament_id=1,
                tournament_status="CANONICALIZED",
                raw_sha256="b" * 64,
                revision_id=2,
            )
        }
    )

    batch = DiscoveryAcquisitionService(registry, acquisition).acquire_candidates(
        [candidate]
    )

    assert acquisition.calls == [candidate.source_url]
    assert batch.results[0].action == "acquired"
    assert batch.acquired == 1
    assert batch.skipped_existing == 0
    assert batch.failed == 0


def test_duplicate_candidate_input_is_acquired_once_in_original_order(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    first = _candidate("tnr1450909")
    duplicate = _candidate("tnr1450909")
    second = _candidate("tnr1450910")
    acquisition = _FakeAcquisition(
        {
            first.source_url: SimpleNamespace(
                tournament_id=1,
                tournament_status="CANONICALIZED",
                raw_sha256="c" * 64,
                revision_id=3,
            ),
            second.source_url: SimpleNamespace(
                tournament_id=2,
                tournament_status="CANONICALIZED",
                raw_sha256="d" * 64,
                revision_id=4,
            ),
        }
    )

    batch = DiscoveryAcquisitionService(registry, acquisition).acquire_candidates(
        [first, duplicate, second]
    )

    assert acquisition.calls == [first.source_url, second.source_url]
    assert batch.discovered == batch.attempted == 2
    assert [result.external_id for result in batch.results] == [
        "tnr1450909",
        "tnr1450910",
    ]


def test_partial_acquisition_failure_does_not_stop_remaining_candidates(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidates = [_candidate("tnr1450909"), _candidate("tnr1450910"), _candidate("tnr1450911")]
    acquisition = _FakeAcquisition(
        {
            candidates[0].source_url: SimpleNamespace(
                tournament_id=1,
                tournament_status="CANONICALIZED",
                raw_sha256="e" * 64,
                revision_id=5,
            ),
            candidates[1].source_url: RuntimeError("provider download failed"),
            candidates[2].source_url: SimpleNamespace(
                tournament_id=3,
                tournament_status="CANONICALIZED",
                raw_sha256="f" * 64,
                revision_id=6,
            ),
        }
    )

    batch = DiscoveryAcquisitionService(registry, acquisition).acquire_candidates(
        candidates
    )

    assert acquisition.calls == [candidate.source_url for candidate in candidates]
    assert batch.discovered == batch.attempted == 3
    assert batch.acquired == 2
    assert batch.skipped_existing == 0
    assert batch.failed == 1
    assert [result.action for result in batch.results] == [
        "acquired",
        "failed",
        "acquired",
    ]
    failure = batch.results[1]
    assert failure.error_type == "RuntimeError"
    assert failure.error_message == "provider download failed"


def test_cli_without_acquire_keeps_read_only_shape_and_does_not_build_b2(
    monkeypatch,
    capsys,
    tmp_path,
):
    from chessgrandmaster.discovery import cli

    candidate = _candidate()

    class _FakeDiscovery:
        def __init__(self, **_kwargs):
            pass

        def expand_family(self, seed, *, limit=None):
            assert seed == "tnr1450909"
            assert limit is None
            from chessgrandmaster.discovery.chess_results import DiscoveryResult

            return DiscoveryResult((candidate,), ())

    class _UnexpectedB2Construction:
        def __init__(self, *args, **kwargs):
            raise AssertionError("B2 must not be constructed without --acquire")

    monkeypatch.setattr(cli, "ChessResultsDiscovery", _FakeDiscovery)
    monkeypatch.setattr(cli, "Registry", _UnexpectedB2Construction, raising=False)
    monkeypatch.setattr(cli, "LocalObjectStore", _UnexpectedB2Construction, raising=False)
    monkeypatch.setattr(cli, "AcquisitionService", _UnexpectedB2Construction, raising=False)
    monkeypatch.setattr(
        cli,
        "DiscoveryAcquisitionService",
        _UnexpectedB2Construction,
        raising=False,
    )

    registry_path = tmp_path / "registry.sqlite"
    root_path = tmp_path / "b2"
    assert (
        cli.main(
            [
                "chess-results",
                "family",
                "tnr1450909",
                "--registry",
                str(registry_path),
                "--root",
                str(root_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["candidate_count"] == 1
    assert "acquisition" not in payload
    assert not registry_path.exists()
    assert not root_path.exists()


def test_cli_acquire_uses_registry_and_root_overrides(monkeypatch, capsys, tmp_path):
    from chessgrandmaster.discovery import cli
    from chessgrandmaster.discovery.acquire import (
        CandidateAcquisition,
        DiscoveryAcquisitionResult,
    )
    from chessgrandmaster.discovery.chess_results import DiscoveryResult

    candidate = _candidate()
    registry_path = tmp_path / "custom" / "registry.sqlite"
    root_path = tmp_path / "custom" / "b2"
    seen: dict[str, object] = {}

    class _FakeDiscovery:
        def __init__(self, **_kwargs):
            pass

        def expand_family(self, seed, *, limit=None):
            return DiscoveryResult((candidate,), ())

    class _FakeRegistry:
        def __init__(self, path):
            seen["registry"] = path

    class _FakeStore:
        def __init__(self, path):
            seen["store"] = path

    class _FakeAcquisition:
        def __init__(self, *, registry, store, workspace, adapters):
            seen["acquisition"] = (registry, store, workspace, adapters)

    class _FakeBridge:
        def __init__(self, registry, acquisition):
            seen["bridge"] = (registry, acquisition)

        def acquire_candidates(self, candidates):
            assert tuple(candidates) == (candidate,)
            return DiscoveryAcquisitionResult(
                discovered=1,
                attempted=1,
                acquired=1,
                skipped_existing=0,
                failed=0,
                results=(
                    CandidateAcquisition(
                        provider=candidate.provider,
                        external_id=candidate.external_id,
                        source_url=candidate.source_url,
                        action="acquired",
                        tournament_id=9,
                        tournament_status="CANONICALIZED",
                        raw_sha256="a" * 64,
                        revision_id=4,
                    ),
                ),
            )

    monkeypatch.setattr(cli, "ChessResultsDiscovery", _FakeDiscovery)
    monkeypatch.setattr(cli, "Registry", _FakeRegistry, raising=False)
    monkeypatch.setattr(cli, "LocalObjectStore", _FakeStore, raising=False)
    monkeypatch.setattr(cli, "AcquisitionService", _FakeAcquisition, raising=False)
    monkeypatch.setattr(
        cli,
        "DiscoveryAcquisitionService",
        _FakeBridge,
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "build_adapters",
        lambda: {"chess-results": "adapter"},
        raising=False,
    )

    assert (
        cli.main(
            [
                "chess-results",
                "family",
                "tnr1450909",
                "--acquire",
                "--registry",
                str(registry_path),
                "--root",
                str(root_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["acquisition"]["acquired"] == 1
    assert payload["acquisition"]["results"][0]["external_id"] == "tnr1450909"
    assert seen["registry"] == registry_path.resolve()
    assert seen["store"] == (root_path / "objects").resolve()
    assert seen["acquisition"][2] == (root_path / "workspace").resolve()
    assert seen["acquisition"][3] == {"chess-results": "adapter"}


def test_cli_acquire_failure_returns_one_json_object_and_exit_one(
    monkeypatch,
    capsys,
    tmp_path,
):
    from chessgrandmaster.discovery import cli
    from chessgrandmaster.discovery.acquire import (
        CandidateAcquisition,
        DiscoveryAcquisitionResult,
    )
    from chessgrandmaster.discovery.chess_results import DiscoveryResult

    first = _candidate("tnr1450909")
    second = _candidate("tnr1450910")

    class _FakeDiscovery:
        def __init__(self, **_kwargs):
            pass

        def expand_family(self, seed, *, limit=None):
            return DiscoveryResult((first, second), ())

    class _FakeBridge:
        def __init__(self, registry, acquisition):
            pass

        def acquire_candidates(self, candidates):
            return DiscoveryAcquisitionResult(
                discovered=2,
                attempted=2,
                acquired=1,
                skipped_existing=0,
                failed=1,
                results=(
                    CandidateAcquisition(
                        provider=first.provider,
                        external_id=first.external_id,
                        source_url=first.source_url,
                        action="acquired",
                        tournament_id=1,
                        tournament_status="CANONICALIZED",
                        raw_sha256="b" * 64,
                        revision_id=1,
                    ),
                    CandidateAcquisition(
                        provider=second.provider,
                        external_id=second.external_id,
                        source_url=second.source_url,
                        action="failed",
                        tournament_id=None,
                        tournament_status=None,
                        raw_sha256=None,
                        revision_id=None,
                        error_type="RuntimeError",
                        error_message="provider failed",
                    ),
                ),
            )

    monkeypatch.setattr(cli, "ChessResultsDiscovery", _FakeDiscovery)
    monkeypatch.setattr(cli, "Registry", lambda path: object(), raising=False)
    monkeypatch.setattr(cli, "LocalObjectStore", lambda path: object(), raising=False)
    monkeypatch.setattr(
        cli,
        "AcquisitionService",
        lambda **kwargs: object(),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "DiscoveryAcquisitionService",
        _FakeBridge,
        raising=False,
    )
    monkeypatch.setattr(cli, "build_adapters", lambda: {}, raising=False)

    assert (
        cli.main(
            [
                "chess-results",
                "family",
                "tnr1450909",
                "--acquire",
                "--registry",
                str(tmp_path / "registry.sqlite"),
                "--root",
                str(tmp_path / "b2"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["acquisition"]["acquired"] == 1
    assert payload["acquisition"]["failed"] == 1
    assert len(payload["acquisition"]["results"]) == 2
