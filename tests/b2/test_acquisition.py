from __future__ import annotations

from collections import Counter
from datetime import date
from io import BytesIO
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import chess.pgn
import pytest

from chessgrandmaster.b2.acquisition import AcquisitionService
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.b2.sources.chess_results import ChessResultsAdapter
from chessgrandmaster.b2.storage import LocalObjectStore
from chessgrandmaster.discovery.acquire import DiscoveryAcquisitionService
from chessgrandmaster.discovery.chess_results import ChessResultsCandidate


FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_URL = (
    "https://s3.chess-results.com/tnr1450909.aspx"
    "?lan=1&art=0&turdet=YES&SNode=S0"
)
NORMALIZED_SOURCE_URL = "https://chess-results.com/tnr1450909.aspx"
SEARCH_URL = "https://chess-results.com/partiesuche.aspx"
DOWNLOAD_URL = "https://chess-results.com/download/fixture-get"
DOWNLOAD_REQUEST_URL = (
    f"{DOWNLOAD_URL}?"
    + urlencode(
        [
            ("format", "pgn"),
            ("session_token", "get-token"),
            ("database_key", "1450909"),
            ("round_from", "1"),
            ("round_to", "7"),
            ("download_action", "Download as PGN-File"),
        ]
    )
)


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        final_url: str | None = None,
        chunk_size: int | None = None,
    ) -> None:
        self._body = BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self.final_url = final_url
        self.chunk_size = chunk_size
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if self.chunk_size is not None and size > self.chunk_size:
            size = self.chunk_size
        return self._body.read(size)

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.final_url or ""

    def close(self) -> None:
        self.closed = True


class FixtureOpener:
    def __init__(
        self,
        pgn_bytes: bytes,
        *,
        fail_download: BaseException | None = None,
    ) -> None:
        self.pgn_bytes = pgn_bytes
        self.fail_download = fail_download
        self.request_records: list[dict[str, object]] = []

    def open(self, request, timeout: float):
        url = request.full_url
        method = request.get_method()
        self.request_records.append(
            {
                "url": url,
                "method": method,
                "data": request.data,
                "timeout": timeout,
            }
        )
        if url == NORMALIZED_SOURCE_URL and method == "GET":
            return FakeResponse(
                b"<html><h1>Fixture tournament</h1>"
                b"<p>Country: VIE Format: classical OTB</p></html>",
                final_url=NORMALIZED_SOURCE_URL,
            )
        if url == SEARCH_URL and method == "GET":
            return FakeResponse(
                (FIXTURES / "chess_results_partiesuche.html").read_bytes(),
                final_url=SEARCH_URL,
            )
        if url == SEARCH_URL and method == "POST":
            return FakeResponse(
                (FIXTURES / "chess_results_database_get.html").read_bytes(),
                final_url=SEARCH_URL,
            )
        if url == DOWNLOAD_REQUEST_URL and method == "GET":
            if self.fail_download is not None:
                raise self.fail_download
            return FakeResponse(
                self.pgn_bytes,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": 'attachment; filename="fixture.pgn"',
                },
                final_url=DOWNLOAD_REQUEST_URL,
                chunk_size=3,
            )
        raise AssertionError(f"unexpected fixture request: {method} {url}")


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _service(
    tmp_path: Path,
    *,
    pgn_bytes: bytes | None = None,
    fail_download: BaseException | None = None,
):
    opener = FixtureOpener(
        pgn_bytes if pgn_bytes is not None else _fixture("chess_results_games.pgn"),
        fail_download=fail_download,
    )
    adapter = ChessResultsAdapter(timeout_sec=7.5, opener=opener)
    registry = Registry(tmp_path / "registry.sqlite")
    store = LocalObjectStore(tmp_path / "objects")
    service = AcquisitionService(
        registry=registry,
        store=store,
        workspace=tmp_path / "workspace",
        adapters={"chess-results": adapter},
    )
    return service, registry, store, opener


def _rows(registry: Registry, table: str) -> list[dict[str, object]]:
    with registry._connect() as connection:
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]


def _count(registry: Registry, table: str) -> int:
    return len(_rows(registry, table))


def _parse_games(path: Path) -> list[chess.pgn.Game]:
    games: list[chess.pgn.Game] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        while True:
            game = chess.pgn.read_game(stream)
            if game is None:
                return games
            games.append(game)


def test_acquire_runs_fixture_backed_source_to_canonical_revision(tmp_path):
    service, registry, store, opener = _service(tmp_path)
    raw_bytes = _fixture("chess_results_games.pgn")

    result = service.acquire("tnr1450909")

    assert result.source_ref.external_id == "tnr1450909"
    assert result.source_ref.source_url == "https://chess-results.com/tnr1450909.aspx"
    assert result.raw_sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert result.raw_byte_size == len(raw_bytes)
    assert result.raw_sha256 in result.raw_object_key
    assert store.stat(result.raw_object_key).sha256 == result.raw_sha256
    restored = tmp_path / "restored-original.pgn"
    store.get_file(result.raw_object_key, restored)
    assert restored.read_bytes() == raw_bytes

    source_rows = _rows(registry, "sources")
    tournament_rows = _rows(registry, "tournaments")
    source_tournament_rows = _rows(registry, "source_tournaments")
    source_file_rows = _rows(registry, "source_files")
    attempt_rows = _rows(registry, "download_attempts")
    revision_rows = _rows(registry, "tournament_revisions")

    assert len(source_rows) == 1
    assert source_rows[0]["name"] == "chess-results"
    assert len(tournament_rows) == 1
    assert tournament_rows[0]["status"] == "CANONICALIZED"
    assert tournament_rows[0]["slug"] == "chess-results-tnr1450909"
    assert len(source_tournament_rows) == 1
    assert source_tournament_rows[0]["external_id"] == "tnr1450909"
    assert source_tournament_rows[0]["source_url"] == result.source_ref.source_url
    assert len(source_file_rows) == 1
    assert source_file_rows[0]["sha256"] == result.raw_sha256
    assert source_file_rows[0]["byte_size"] == len(raw_bytes)
    assert source_file_rows[0]["object_key"] == result.raw_object_key
    assert len(attempt_rows) == 1
    assert attempt_rows[0]["finished_at"] is not None
    assert attempt_rows[0]["error"] is None
    assert attempt_rows[0]["source_file_id"] == result.source_file_id
    assert len(revision_rows) == 1
    assert revision_rows[0]["id"] == result.revision_id
    assert result.revision_number == 1
    assert registry.get_tournament_status(result.tournament_id) == "CANONICALIZED"

    canonical_games = _parse_games(result.canonical_path)
    assert len(canonical_games) == result.canonical_game_count
    assert result.canonical_game_count > 0
    assert all(not game.errors for game in canonical_games)
    assert result.canonical_ply_count == sum(game.end().ply() for game in canonical_games)
    membership = registry.get_revision_games(result.revision_id)
    assert len(membership) == result.canonical_game_count
    assert [record["url"] for record in opener.request_records] == [
        NORMALIZED_SOURCE_URL,
        SEARCH_URL,
        SEARCH_URL,
        DOWNLOAD_REQUEST_URL,
    ]
    assert [record["method"] for record in opener.request_records] == [
        "GET",
        "GET",
        "POST",
        "GET",
    ]


def test_repeat_acquisition_reuses_all_content_and_revision_rows(tmp_path):
    service, registry, store, _ = _service(tmp_path)

    first = service.acquire("tnr1450909")
    counts_after_first = {
        table: _count(registry, table)
        for table in (
            "sources",
            "tournaments",
            "source_tournaments",
            "source_files",
            "canonical_games",
            "game_occurrences",
            "game_metadata_conflicts",
            "tournament_revisions",
            "tournament_games",
        )
    }
    objects_after_first = store.list("")

    second = service.acquire("tnr1450909")

    counts_after_second = {
        table: _count(registry, table)
        for table in counts_after_first
    }
    assert counts_after_second == counts_after_first
    assert store.list("") == objects_after_first
    assert _count(registry, "download_attempts") == 2
    assert (
        first.tournament_id,
        first.source_file_id,
        first.raw_sha256,
        first.canonical_sha256,
        first.revision_id,
        first.revision_number,
    ) == (
        second.tournament_id,
        second.source_file_id,
        second.raw_sha256,
        second.canonical_sha256,
        second.revision_id,
        second.revision_number,
    )
    assert second.canonical_path.read_bytes() == first.canonical_path.read_bytes()


def test_new_raw_bytes_preserve_old_source_and_create_new_revision(tmp_path):
    first_service, registry, store, _ = _service(tmp_path)
    first = first_service.acquire("tnr1450909")
    old_raw_bytes = _fixture("chess_results_games.pgn")
    old_object_key = first.raw_object_key
    old_revision_id = first.revision_id
    old_source_file_id = first.source_file_id

    extra_game = (
        b'\n[Event "Added"]\n[Site "Fixture"]\n[Date "2026.09.14"]\n'
        b'[Round "99"]\n[White "Extra White"]\n[Black "Extra Black"]\n'
        b'[Result "1-0"]\n\n1. d4 d5 2. c4 e6 1-0\n'
    )
    second_service, _, _, _ = _service(
        tmp_path,
        pgn_bytes=old_raw_bytes + extra_game,
    )
    second = second_service.acquire("tnr1450909")

    assert second.tournament_id == first.tournament_id
    assert second.source_tournament_id == first.source_tournament_id
    assert second.raw_sha256 != first.raw_sha256
    assert second.source_file_id != old_source_file_id
    assert store.exists(old_object_key)
    assert store.stat(old_object_key).sha256 == first.raw_sha256
    assert store.exists(second.raw_object_key)
    assert _count(registry, "source_files") == 2
    assert _count(registry, "tournament_revisions") == 2
    assert second.revision_id != old_revision_id
    assert second.revision_number == 2
    assert second.canonical_sha256 != first.canonical_sha256
    assert len(registry.get_revision_games(old_revision_id)) == first.canonical_game_count
    assert len(registry.get_revision_games(second.revision_id)) == second.canonical_game_count


def test_unsupported_query_has_no_phantom_successful_acquisition(tmp_path):
    service, registry, store, _ = _service(tmp_path)

    with pytest.raises(ValueError, match="no adapter|discovery|source"):
        service.acquire("not-a-source")

    assert all(_count(registry, table) == 0 for table in ("sources", "tournaments", "source_tournaments", "source_files", "download_attempts"))
    assert store.list("") == []


def test_download_failure_records_failed_attempt_without_source_file(tmp_path):
    service, registry, store, _ = _service(
        tmp_path,
        fail_download=RuntimeError("fixture network failure"),
    )

    with pytest.raises(RuntimeError, match="fixture network failure"):
        service.acquire("tnr1450909")

    assert _count(registry, "sources") == 1
    assert _count(registry, "tournaments") == 1
    assert _count(registry, "source_tournaments") == 1
    assert _count(registry, "download_attempts") == 1
    assert _count(registry, "source_files") == 0
    attempt = _rows(registry, "download_attempts")[0]
    assert attempt["finished_at"] is not None
    assert "fixture network failure" in str(attempt["error"])
    assert registry.get_tournament_status(1) == "DISCOVERED"
    assert store.list("") == []


def test_zero_valid_game_preserves_raw_source_but_stops_at_downloaded(tmp_path):
    invalid_pgn = (
        b'[Event "Invalid"]\n[Result "*"]\n\n'
        b"1. e4 e5 2. e4 *\n"
    )
    service, registry, store, _ = _service(tmp_path, pgn_bytes=invalid_pgn)

    with pytest.raises(ValueError, match="legal game|valid game"):
        service.acquire("tnr1450909")

    source_file = _rows(registry, "source_files")
    assert len(source_file) == 1
    assert source_file[0]["sha256"] == hashlib.sha256(invalid_pgn).hexdigest()
    assert source_file[0]["byte_size"] == len(invalid_pgn)
    assert store.stat(str(source_file[0]["object_key"])).sha256 == source_file[0]["sha256"]
    assert registry.get_tournament_status(1) == "DOWNLOADED"
    assert _count(registry, "tournament_revisions") == 0
    assert _count(registry, "canonical_games") == 0


def test_canonicalization_failure_preserves_raw_source_and_prior_revision(tmp_path, monkeypatch):
    service, registry, store, _ = _service(tmp_path)
    first = service.acquire("tnr1450909")
    old_revision_rows = _rows(registry, "tournament_revisions")
    old_object_key = first.raw_object_key

    def fail_canonicalize(*args, **kwargs):
        raise RuntimeError("fixture canonicalizer failure")

    monkeypatch.setattr(
        "chessgrandmaster.b2.acquisition.canonicalize_source_file",
        fail_canonicalize,
    )
    failing_service, _, _, _ = _service(tmp_path)

    with pytest.raises(RuntimeError, match="fixture canonicalizer failure"):
        failing_service.acquire("tnr1450909")

    assert store.exists(old_object_key)
    assert _rows(registry, "tournament_revisions") == old_revision_rows
    assert registry.get_tournament_status(first.tournament_id) == "CANONICALIZED"
    assert _count(registry, "source_files") == 1
    assert _count(registry, "download_attempts") == 2
    assert _rows(registry, "download_attempts")[-1]["error"] is None


def test_result_status_reflects_actual_registry_state_without_regression(tmp_path):
    service, registry, _, _ = _service(tmp_path)

    first = service.acquire("tnr1450909")
    assert first.tournament_status == "CANONICALIZED"

    registry.set_tournament_status(first.tournament_id, "SHARDED")
    registry.set_tournament_status(first.tournament_id, "READY")

    second = service.acquire("tnr1450909")

    assert registry.get_tournament_status(first.tournament_id) == "READY"
    assert second.tournament_status == "READY"
    assert second.tournament_id == first.tournament_id
    assert second.revision_id == first.revision_id
    assert second.canonical_sha256 == first.canonical_sha256


def test_discovery_acquisition_rerun_skips_completed_source_without_new_revision(
    tmp_path,
):
    acquisition, registry, _store, _opener = _service(tmp_path)
    candidate = ChessResultsCandidate(
        provider="chess-results",
        external_id="tnr1450909",
        source_url=NORMALIZED_SOURCE_URL,
        title="Fixture tournament",
        time_control_hint="standard",
        evidence=("fixture",),
    )

    class CountingAcquisition:
        def __init__(self, delegate):
            self.delegate = delegate
            self.calls: list[str] = []

        def acquire(self, source_url: str):
            self.calls.append(source_url)
            return self.delegate.acquire(source_url)

    counting = CountingAcquisition(acquisition)
    adapter = acquisition.adapters["chess-results"]

    def probe(candidate):
        ref = adapter.discover(candidate.source_url)[0]
        return adapter.probe_pgn(ref)

    bridge = DiscoveryAcquisitionService(registry, counting, probe)

    first = bridge.acquire_candidates([candidate])
    counts_after_first = registry.registry_counts()
    second = bridge.acquire_candidates([candidate])
    counts_after_second = registry.registry_counts()

    assert first.results[0].action == "acquired"
    assert first.results[0].tournament_status == "CANONICALIZED"
    assert first.results[0].raw_sha256 is not None
    assert first.results[0].revision_id is not None
    assert counts_after_first["download_attempts"] == 1
    assert counts_after_first["tournament_revisions"] == 1
    assert counts_after_first["canonical_games"] > 0

    assert second.results[0].action == "skipped_existing"
    assert second.results[0].tournament_id == first.results[0].tournament_id
    assert second.results[0].tournament_status == "CANONICALIZED"
    assert counting.calls == [candidate.source_url]
    assert counts_after_second == counts_after_first


def _refresh_candidate() -> ChessResultsCandidate:
    return ChessResultsCandidate(
        provider="chess-results",
        external_id="tnr1450909",
        source_url=NORMALIZED_SOURCE_URL,
        title="Fixture tournament",
        time_control_hint="standard",
        evidence=("fixture",),
        to_date="2026-09-13",
    )


def _bridge_for_fixture(acquisition, registry):
    adapter = acquisition.adapters["chess-results"]

    def probe(candidate):
        ref = adapter.discover(candidate.source_url)[0]
        return adapter.probe_pgn(ref)

    return DiscoveryAcquisitionService(registry, acquisition, probe)


def test_recent_refresh_unchanged_reuses_raw_source_and_revision(tmp_path):
    acquisition, registry, store, _ = _service(tmp_path)
    candidate = _refresh_candidate()

    first = _bridge_for_fixture(acquisition, registry).acquire_candidates([candidate])
    counts_after_first = registry.registry_counts()
    second = _bridge_for_fixture(acquisition, registry).acquire_candidates(
        [candidate],
        refresh_recent_days=60,
        as_of=date(2026, 9, 16),
    )
    counts_after_second = registry.registry_counts()

    assert first.results[0].action == "acquired"
    assert second.results[0].action == "refreshed_unchanged"
    assert second.results[0].previous_raw_sha256 == first.results[0].raw_sha256
    assert second.results[0].raw_sha256 == first.results[0].raw_sha256
    assert counts_after_second["download_attempts"] == (
        counts_after_first["download_attempts"] + 1
    )
    assert counts_after_second["source_files"] == counts_after_first["source_files"] == 1
    assert (
        counts_after_second["tournament_revisions"]
        == counts_after_first["tournament_revisions"]
        == 1
    )
    assert counts_after_second["canonical_games"] == counts_after_first["canonical_games"]
    assert any(
        first.results[0].raw_sha256 in key for key in store.list("")
    )


def test_recent_refresh_with_added_game_preserves_old_source_and_revision(tmp_path):
    initial_pgn = _fixture("chess_results_games.pgn")
    first_service, registry, store, _ = _service(tmp_path, pgn_bytes=initial_pgn)
    candidate = _refresh_candidate()
    first = _bridge_for_fixture(first_service, registry).acquire_candidates([candidate])

    updated_pgn = initial_pgn + (
        b'\n[Event "Added"]\n[Site "Fixture"]\n[Date "2026.09.14"]\n'
        b'[Round "99"]\n[White "Extra White"]\n[Black "Extra Black"]\n'
        b'[Result "1-0"]\n\n1. d4 d5 2. c4 e6 1-0\n'
    )
    second_service, _, _, _ = _service(tmp_path, pgn_bytes=updated_pgn)
    second = _bridge_for_fixture(second_service, registry).acquire_candidates(
        [candidate],
        refresh_recent_days=60,
        as_of=date(2026, 9, 16),
    )

    assert second.results[0].action == "refreshed_changed"
    assert second.results[0].previous_raw_sha256 == first.results[0].raw_sha256
    assert second.results[0].raw_sha256 != first.results[0].raw_sha256
    object_keys = store.list("")
    assert any(first.results[0].raw_sha256 in key for key in object_keys)
    assert any(second.results[0].raw_sha256 in key for key in object_keys)
    assert registry.registry_counts()["source_files"] == 2
    assert registry.registry_counts()["tournament_revisions"] == 2
    assert second.results[0].revision_id != first.results[0].revision_id



def test_recent_refresh_raw_change_can_reuse_canonical_revision(tmp_path):
    initial_pgn = _fixture("chess_results_games.pgn")
    first_service, registry, store, _ = _service(tmp_path, pgn_bytes=initial_pgn)
    candidate = _refresh_candidate()
    first = _bridge_for_fixture(first_service, registry).acquire_candidates([candidate])

    updated_pgn = initial_pgn + b"\n; provider-only note\n"
    second_service, _, _, _ = _service(tmp_path, pgn_bytes=updated_pgn)
    second = _bridge_for_fixture(second_service, registry).acquire_candidates(
        [candidate],
        refresh_recent_days=60,
        as_of=date(2026, 9, 16),
    )

    assert second.results[0].action == "refreshed_changed"
    assert second.results[0].raw_sha256 != first.results[0].raw_sha256
    object_keys = store.list("")
    assert any(first.results[0].raw_sha256 in key for key in object_keys)
    assert any(second.results[0].raw_sha256 in key for key in object_keys)
    assert registry.registry_counts()["source_files"] == 2
    assert registry.registry_counts()["tournament_revisions"] == 1
    assert second.results[0].revision_id == first.results[0].revision_id


def test_fetch_only_registers_raw_source_without_validation_or_canonicalization(
    tmp_path,
    monkeypatch,
):
    from chessgrandmaster.b2 import acquisition as acquisition_module

    service, registry, store, _ = _service(tmp_path)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("fetch-only acquisition must not parse or canonicalize")

    monkeypatch.setattr(acquisition_module, "_validation_summary", unexpected)
    monkeypatch.setattr(acquisition_module, "canonicalize_source_file", unexpected)
    monkeypatch.setattr(acquisition_module, "identify_game", unexpected)

    result = service.fetch("tnr1450909")

    assert result.tournament_status == "DOWNLOADED"
    assert result.source_file_id > 0
    assert result.raw_sha256 in result.raw_object_key
    assert store.stat(result.raw_object_key).sha256 == result.raw_sha256
    assert registry.get_tournament_status(result.tournament_id) == "DOWNLOADED"
    assert registry.registry_counts()["canonical_games"] == 0
    assert registry.registry_counts()["game_occurrences"] == 0
    assert registry.registry_counts()["tournament_revisions"] == 0


def test_fetch_bridge_rerun_skips_downloaded_candidate_when_refresh_is_disabled(
    tmp_path,
):
    service, registry, _store, _opener = _service(tmp_path)
    candidate = ChessResultsCandidate(
        provider="chess-results",
        external_id="tnr1450909",
        source_url=NORMALIZED_SOURCE_URL,
        title="Fixture tournament",
        time_control_hint="standard",
        evidence=("fixture",),
    )
    adapter = service.adapters["chess-results"]

    def probe(probe_candidate):
        ref = adapter.discover(probe_candidate.source_url)[0]
        return adapter.probe_pgn(ref)

    bridge = DiscoveryAcquisitionService(registry, service, probe)
    first = bridge.fetch_candidates([candidate], refresh_recent_days=0)
    second = bridge.fetch_candidates([candidate], refresh_recent_days=0)

    assert first.results[0].action == "fetched"
    assert second.results[0].action == "skipped_existing"
    assert second.results[0].tournament_status == "DOWNLOADED"
    assert registry.registry_counts()["download_attempts"] == 1
    assert registry.registry_counts()["source_files"] == 1


@pytest.mark.parametrize("bad_bytes", [b"", b"<html><body>provider error</body></html>"])
def test_fetch_rejects_empty_or_html_provider_response_without_raw_provenance(
    tmp_path,
    bad_bytes,
):
    service, registry, store, _ = _service(tmp_path, pgn_bytes=bad_bytes)

    with pytest.raises(RuntimeError, match="empty|HTML|PGN"):
        service.fetch("tnr1450909")

    assert registry.registry_counts()["source_files"] == 0
    assert registry.registry_counts()["canonical_games"] == 0
    assert registry.registry_counts()["game_occurrences"] == 0
    assert registry.registry_counts()["tournament_revisions"] == 0
    assert store.list("") == []


def test_fetch_empty_pgn_error_is_typed_and_attempt_remains_auditable(tmp_path):
    service, registry, store, _ = _service(tmp_path, pgn_bytes=b"")

    with pytest.raises(RuntimeError) as raised:
        service.fetch("tnr1450909")

    assert getattr(raised.value, "reason", None) == "empty_pgn"
    assert str(raised.value) == "empty_pgn"
    attempt = _rows(registry, "download_attempts")[0]
    assert attempt["source_file_id"] is None
    assert attempt["error"] == "empty_pgn"
    assert registry.registry_counts()["source_files"] == 0
    assert store.list("") == []
