from __future__ import annotations

from datetime import date
import json
from types import SimpleNamespace

import pytest

from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.discovery.chess_results import ChessResultsCandidate
from chessgrandmaster.discovery.acquire import DiscoveryAcquisitionService


SOURCE_URL = "https://chess-results.com/tnr1450909.aspx?lan=1"


def _candidate(
    external_id: str = "tnr1450909",
    *,
    to_date: str | None = None,
) -> ChessResultsCandidate:
    return ChessResultsCandidate(
        provider="chess-results",
        external_id=external_id,
        source_url=SOURCE_URL.replace("1450909", external_id.removeprefix("tnr")),
        title="Fixture tournament",
        time_control_hint="standard",
        evidence=("fixture",),
        to_date=to_date,
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


class _FakePgnProbe:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls: list[str] = []

    def __call__(self, candidate):
        self.calls.append(candidate.source_url)
        outcome = self.outcomes[candidate.source_url]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _available_probe(_candidate):
    return SimpleNamespace(
        available=True,
        database_key="1450909",
        round_count=9,
        game_count=72,
        reason=None,
    )


def _register_candidate(
    registry: Registry,
    candidate: ChessResultsCandidate,
    status: str,
    *,
    end_date: str | None = None,
) -> int:
    source_id = registry.upsert_source(
        candidate.provider,
        "https://chess-results.com",
    )
    tournament_id = registry.upsert_tournament(
        f"{candidate.provider}-{candidate.external_id}",
        end_date=end_date,
        status=status,
    )
    registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id=candidate.external_id,
        source_url=candidate.source_url,
    )
    return tournament_id


def _record_raw_source(
    registry: Registry,
    candidate: ChessResultsCandidate,
    sha256: str,
) -> int:
    existing = registry.find_source_tournament(
        candidate.provider,
        candidate.external_id,
    )
    assert existing is not None
    return registry.record_source_file(
        source_tournament_id=int(existing["source_tournament_id"]),
        object_key=f"raw/{sha256}/fixture.pgn",
        filename="fixture.pgn",
        sha256=sha256,
        byte_size=10,
    )


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
    service = DiscoveryAcquisitionService(registry, acquisition, _available_probe)

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

    batch = DiscoveryAcquisitionService(registry, acquisition, _available_probe).acquire_candidates(
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

    batch = DiscoveryAcquisitionService(registry, acquisition, _available_probe).acquire_candidates(
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

    batch = DiscoveryAcquisitionService(registry, acquisition, _available_probe).acquire_candidates(
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

    batch = DiscoveryAcquisitionService(registry, acquisition, _available_probe).acquire_candidates(
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


def test_no_pgn_candidate_is_skipped_before_acquisition_or_registry_mutation(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate()
    acquisition = _FakeAcquisition({})
    probe = _FakePgnProbe(
        {
            candidate.source_url: SimpleNamespace(
                available=False,
                database_key="1450909",
                round_count=0,
                game_count=0,
                reason="no_game_database",
            )
        }
    )
    before = registry.registry_counts()

    batch = DiscoveryAcquisitionService(
        registry,
        acquisition,
        probe,
    ).acquire_candidates([candidate])

    assert probe.calls == [candidate.source_url]
    assert acquisition.calls == []
    assert batch.results[0].action == "skipped_no_pgn"
    assert batch.skipped_no_pgn == 1
    assert batch.failed == 0
    assert registry.registry_counts() == before


def test_available_candidate_is_probed_then_acquired_once(tmp_path):
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
    probe = _FakePgnProbe(
        {
            candidate.source_url: SimpleNamespace(
                available=True,
                database_key="1450909",
                round_count=9,
                game_count=72,
                reason=None,
            )
        }
    )

    batch = DiscoveryAcquisitionService(
        registry,
        acquisition,
        probe,
    ).acquire_candidates([candidate])

    assert probe.calls == [candidate.source_url]
    assert acquisition.calls == [candidate.source_url]
    assert batch.results[0].action == "acquired"
    assert batch.acquired == 1
    assert batch.skipped_no_pgn == 0
    assert batch.failed == 0


def test_mixed_batch_preserves_no_pgn_and_probe_failures_without_stopping(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidates = [
        _candidate("tnr1450909"),
        _candidate("tnr1450910"),
        _candidate("tnr1450911"),
        _candidate("tnr1450912"),
        _candidate("tnr1450913"),
    ]
    _register_candidate(registry, candidates[2], "CANONICALIZED")
    acquisition = _FakeAcquisition(
        {
            candidates[1].source_url: SimpleNamespace(
                tournament_id=2,
                tournament_status="CANONICALIZED",
                raw_sha256="b" * 64,
                revision_id=2,
            ),
            candidates[4].source_url: SimpleNamespace(
                tournament_id=5,
                tournament_status="CANONICALIZED",
                raw_sha256="e" * 64,
                revision_id=5,
            ),
        }
    )
    probe = _FakePgnProbe(
        {
            candidates[0].source_url: SimpleNamespace(available=False),
            candidates[1].source_url: SimpleNamespace(available=True),
            candidates[3].source_url: RuntimeError("probe HTTP failed"),
            candidates[4].source_url: SimpleNamespace(available=True),
        }
    )

    batch = DiscoveryAcquisitionService(
        registry,
        acquisition,
        probe,
    ).acquire_candidates(candidates)

    assert probe.calls == [
        candidates[0].source_url,
        candidates[1].source_url,
        candidates[3].source_url,
        candidates[4].source_url,
    ]
    assert acquisition.calls == [candidates[1].source_url, candidates[4].source_url]
    assert [result.action for result in batch.results] == [
        "skipped_no_pgn",
        "acquired",
        "skipped_existing",
        "failed",
        "acquired",
    ]
    assert batch.discovered == batch.attempted == 5
    assert batch.acquired == 2
    assert batch.skipped_no_pgn == 1
    assert batch.skipped_existing == 1
    assert batch.failed == 1
    assert batch.results[3].error_type == "RuntimeError"
    assert batch.results[3].error_message == "probe HTTP failed"


def test_completed_candidate_skips_before_availability_probe_on_rerun(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate()
    _register_candidate(registry, candidate, "CANONICALIZED")
    acquisition = _FakeAcquisition({})
    probe = _FakePgnProbe(
        {candidate.source_url: AssertionError("completed candidate was probed")}
    )

    batch = DiscoveryAcquisitionService(
        registry,
        acquisition,
        probe,
    ).acquire_candidates([candidate])

    assert probe.calls == []
    assert acquisition.calls == []
    assert batch.results[0].action == "skipped_existing"
    assert batch.skipped_no_pgn == 0
    assert batch.failed == 0


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
        def __init__(self, registry, acquisition, pgn_probe):
            seen["bridge"] = (registry, acquisition, pgn_probe)

        def acquire_candidates(self, candidates):
            assert tuple(candidates) == (candidate,)
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
        def __init__(self, registry, acquisition, pgn_probe):
            pass

        def acquire_candidates(self, candidates):
            return DiscoveryAcquisitionResult(
                discovered=2,
                attempted=2,
                acquired=1,
                skipped_existing=0,
                skipped_no_pgn=0,
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


def test_late_pgn_availability_is_retried_without_persistent_negative_state(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate()
    acquisition = _FakeAcquisition(
        {
            candidate.source_url: SimpleNamespace(
                tournament_id=1,
                tournament_status="CANONICALIZED",
                raw_sha256="a" * 64,
                revision_id=1,
            )
        }
    )
    probe_results = iter(
        [
            SimpleNamespace(
                available=False,
                database_key="1450909",
                round_count=0,
                game_count=0,
                reason="no_game_database",
            ),
            _available_probe(candidate),
        ]
    )
    probe = lambda _candidate: next(probe_results)
    service = DiscoveryAcquisitionService(registry, acquisition, probe)

    first = service.acquire_candidates([candidate])
    second = service.acquire_candidates([candidate])

    assert first.results[0].action == "skipped_no_pgn"
    assert second.results[0].action == "acquired"
    assert acquisition.calls == [candidate.source_url]
    assert registry.find_source_tournament(candidate.provider, candidate.external_id) is None


def test_recent_canonicalized_refresh_unchanged_tracks_previous_raw_sha(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate(to_date="2026-09-13")
    _register_candidate(
        registry,
        candidate,
        "CANONICALIZED",
        end_date=candidate.to_date,
    )
    _record_raw_source(registry, candidate, "a" * 64)
    acquisition = _FakeAcquisition(
        {
            candidate.source_url: SimpleNamespace(
                tournament_id=1,
                tournament_status="CANONICALIZED",
                raw_sha256="a" * 64,
                revision_id=7,
            )
        }
    )
    probe = _FakePgnProbe({candidate.source_url: _available_probe(candidate)})

    batch = DiscoveryAcquisitionService(registry, acquisition, probe).acquire_candidates(
        [candidate],
        refresh_recent_days=60,
        as_of=date(2026, 9, 16),
    )

    outcome = batch.results[0]
    assert outcome.action == "refreshed_unchanged"
    assert outcome.previous_raw_sha256 == "a" * 64
    assert outcome.raw_sha256 == "a" * 64
    assert batch.acquired == 0
    assert batch.refreshed_unchanged == 1
    assert batch.refreshed_changed == 0
    assert batch.refresh_unavailable == 0
    assert probe.calls == [candidate.source_url]
    assert acquisition.calls == [candidate.source_url]


@pytest.mark.parametrize(
    ("end_date", "action", "probe_called"),
    [
        ("2026-07-18", "refreshed_unchanged", True),
        ("2026-07-17", "skipped_existing", False),
    ],
)
def test_recent_refresh_uses_inclusive_60_day_boundary(
    tmp_path,
    end_date,
    action,
    probe_called,
):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate(to_date=end_date)
    _register_candidate(registry, candidate, "CANONICALIZED", end_date=end_date)
    _record_raw_source(registry, candidate, "a" * 64)
    acquisition = _FakeAcquisition(
        {
            candidate.source_url: SimpleNamespace(
                tournament_id=1,
                tournament_status="CANONICALIZED",
                raw_sha256="a" * 64,
                revision_id=7,
            )
        }
    )
    probe = _FakePgnProbe({candidate.source_url: _available_probe(candidate)})

    batch = DiscoveryAcquisitionService(registry, acquisition, probe).acquire_candidates(
        [candidate],
        refresh_recent_days=60,
        as_of=date(2026, 9, 16),
    )

    assert batch.results[0].action == action
    assert bool(probe.calls) is probe_called
    assert bool(acquisition.calls) is probe_called


def test_refresh_disabled_preserves_completed_skip_semantics(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate(to_date="2026-09-13")
    tournament_id = _register_candidate(
        registry,
        candidate,
        "CANONICALIZED",
        end_date=candidate.to_date,
    )
    probe = _FakePgnProbe({})
    acquisition = _FakeAcquisition({})

    batch = DiscoveryAcquisitionService(registry, acquisition, probe).acquire_candidates(
        [candidate],
        refresh_recent_days=0,
        as_of=date(2026, 9, 16),
    )

    assert batch.results[0].action == "skipped_existing"
    assert batch.results[0].tournament_id == tournament_id
    assert probe.calls == []
    assert acquisition.calls == []


@pytest.mark.parametrize("status", ["SHARDED", "READY"])
def test_recent_sharded_or_ready_tournament_is_not_refreshed(tmp_path, status):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate(to_date="2026-09-13")
    _register_candidate(registry, candidate, status, end_date=candidate.to_date)
    probe = _FakePgnProbe({})
    acquisition = _FakeAcquisition({})

    batch = DiscoveryAcquisitionService(registry, acquisition, probe).acquire_candidates(
        [candidate],
        refresh_recent_days=60,
        as_of=date(2026, 9, 16),
    )

    assert batch.results[0].action == "skipped_existing"
    assert probe.calls == []
    assert acquisition.calls == []


@pytest.mark.parametrize("to_date", [None, "not-a-date", "2026-09-17"])
def test_missing_malformed_or_future_end_date_does_not_force_refresh(
    tmp_path,
    to_date,
):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate(to_date=to_date)
    _register_candidate(registry, candidate, "CANONICALIZED", end_date=to_date)
    probe = _FakePgnProbe({})
    acquisition = _FakeAcquisition({})

    batch = DiscoveryAcquisitionService(registry, acquisition, probe).acquire_candidates(
        [candidate],
        refresh_recent_days=60,
        as_of=date(2026, 9, 16),
    )

    assert batch.results[0].action == "skipped_existing"
    assert probe.calls == []
    assert acquisition.calls == []


def test_recent_refresh_unavailable_preserves_existing_registry_content(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite")
    candidate = _candidate(to_date="2026-09-13")
    tournament_id = _register_candidate(
        registry,
        candidate,
        "CANONICALIZED",
        end_date=candidate.to_date,
    )
    _record_raw_source(registry, candidate, "a" * 64)
    before = registry.registry_counts()
    probe = _FakePgnProbe(
        {
            candidate.source_url: SimpleNamespace(
                available=False,
                database_key="1450909",
                round_count=0,
                game_count=0,
                reason="no_game_database",
            )
        }
    )
    acquisition = _FakeAcquisition({})

    batch = DiscoveryAcquisitionService(registry, acquisition, probe).acquire_candidates(
        [candidate],
        refresh_recent_days=60,
        as_of=date(2026, 9, 16),
    )

    assert batch.results[0].action == "refresh_unavailable"
    assert batch.results[0].previous_raw_sha256 == "a" * 64
    assert batch.refresh_unavailable == 1
    assert acquisition.calls == []
    assert registry.registry_counts() == before
    assert registry.get_tournament_status(tournament_id) == "CANONICALIZED"
