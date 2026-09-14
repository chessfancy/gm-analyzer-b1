from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from chessgrandmaster.b2.sources.base import SourceRef
from chessgrandmaster.b2.sources.chess_results import ChessResultsAdapter


FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://s3.chess-results.com/tnr1450909.aspx?lan=1"
PGN_URL = "https://s3.chess-results.com/DownloadFile.aspx?file=Games.pgn"


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        chunk_size: int | None = None,
        error_after: int | None = None,
    ):
        self._body = BytesIO(body)
        self.status = status
        self.chunk_size = chunk_size
        self.error_after = error_after
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if (
            self.error_after is not None
            and self._body.tell() >= self.error_after
        ):
            raise TimeoutError("truncated fixture response")
        if self.chunk_size is not None and size > self.chunk_size:
            size = self.chunk_size
        return self._body.read(size)

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, float, str | None]] = []
        self.opened_responses: list[tuple[str, FakeResponse]] = []

    def open(self, request, timeout):
        url = request.full_url
        self.calls.append((url, timeout, request.get_header("User-agent")))
        response = self.responses[url]
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        self.opened_responses.append((url, response))
        return response


def _adapter_for_page(page: bytes | None = None, *, pgn: bytes | None = None):
    page = page if page is not None else (
        FIXTURES / "chess_results_tournament.html"
    ).read_bytes()
    responses = {PAGE_URL: lambda: FakeResponse(page)}
    if pgn is not None:
        responses[PGN_URL] = lambda: FakeResponse(pgn, chunk_size=3)
    opener = FakeOpener(responses)
    return ChessResultsAdapter(timeout_sec=7.5, opener=opener), opener


def test_discover_literal_normalizes_to_one_source_ref():
    adapter, _ = _adapter_for_page()

    refs = adapter.discover("tnr1450909")

    assert refs == [
        SourceRef(
            provider="chess-results",
            external_id="tnr1450909",
            source_url="https://chess-results.com/tnr1450909.aspx",
        )
    ]


@pytest.mark.parametrize(
    "source_url",
    [
        "https://chess-results.com/tnr1450909.aspx?lan=1",
        "https://www.chess-results.com/tnr1450909.aspx?lan=1",
        "https://s1.chess-results.com/tnr1450909.aspx?lan=1",
        "https://s2.chess-results.com/tnr1450909.aspx?lan=1",
        "https://s3.chess-results.com/tnr1450909.aspx?lan=1",
        "https://s123.chess-results.com/test/tnr1450909.aspx?lan=1",
    ],
)
def test_discover_preserves_supported_absolute_url_provenance(source_url):
    adapter, _ = _adapter_for_page()

    ref = adapter.discover(source_url)[0]

    assert ref.provider == "chess-results"
    assert ref.external_id == "tnr1450909"
    assert ref.source_url == source_url


def test_discover_rejects_unrelated_domains_and_free_text():
    adapter, _ = _adapter_for_page()

    for query in (
        "youth tournament",
        "tnrABC",
        "/tnr1450909.aspx",
        "https://example.com/tnr1450909.aspx",
        "https://not-chess-results.com/tnr1450909.aspx",
        "https://s3.chess-results.com/tnr1450909.aspx/extra",
    ):
        with pytest.raises(ValueError):
            adapter.discover(query)


def test_describe_extracts_title_and_resolves_relative_pgn_link():
    adapter, opener = _adapter_for_page()
    ref = adapter.discover(PAGE_URL)[0]

    descriptor = adapter.describe(ref)

    assert descriptor.ref == ref
    assert descriptor.title == "Giải Vô địch Cờ vua trẻ quốc gia năm 2026"
    assert descriptor.pgn_url == PGN_URL
    assert descriptor.event_country_hint is None
    assert descriptor.time_control_hint is None
    assert descriptor.is_otb_hint is None
    assert opener.calls[0][0] == PAGE_URL
    assert opener.calls[0][1] == 7.5
    assert opener.calls[0][2] == "ChessGrandmaster acquisition/1.0"


def test_describe_accepts_direct_pgn_link():
    page = b'<html><h1>Direct fixture</h1><a href="https://cdn.example/games.pgn">PGN</a></html>'
    adapter, _ = _adapter_for_page(page)
    ref = adapter.discover(PAGE_URL)[0]

    descriptor = adapter.describe(ref)

    assert descriptor.pgn_url == "https://cdn.example/games.pgn"


def test_describe_does_not_infer_domestic_codes_as_vietnamese_country():
    page = b"""
    <html><body>
      <h2>Domestic event</h2>
      <table><tr><th>FED</th><td>HCM</td><td>HNO</td><td>DAN</td></tr></table>
    </body></html>
    """
    adapter, _ = _adapter_for_page(page)
    ref = adapter.discover(PAGE_URL)[0]

    descriptor = adapter.describe(ref)

    assert descriptor.event_country_hint is None
    assert descriptor.time_control_hint is None
    assert descriptor.is_otb_hint is None


def test_missing_pgn_link_is_explicit_and_download_refuses_to_guess(tmp_path):
    page = b"<html><h2>Tournament without games</h2><a href='/players'>Players</a></html>"
    adapter, opener = _adapter_for_page(page)
    ref = adapter.discover(PAGE_URL)[0]
    destination = tmp_path / "games.pgn"

    descriptor = adapter.describe(ref)
    assert descriptor.pgn_url is None

    with pytest.raises(
        RuntimeError,
        match="Chess-Results page exposes no downloadable PGN link",
    ):
        adapter.download_pgn(ref, destination)

    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()
    assert [call[0] for call in opener.calls] == [PAGE_URL, PAGE_URL]


def test_download_pgn_is_streamed_and_installed_atomically(tmp_path):
    pgn = (FIXTURES / "chess_results_games.pgn").read_bytes()
    adapter, opener = _adapter_for_page(pgn=pgn)
    ref = adapter.discover(PAGE_URL)[0]
    destination = tmp_path / "nested" / "games.pgn"

    result = adapter.download_pgn(ref, destination)

    assert result == destination
    assert destination.read_bytes() == pgn
    assert not Path(f"{destination}.part").exists()
    assert [call[0] for call in opener.calls] == [PAGE_URL, PGN_URL]
    pgn_response = [
        response
        for url, response in opener.opened_responses
        if url == PGN_URL
    ][0]
    assert pgn_response.read_sizes
    assert all(size == 1024 * 1024 for size in pgn_response.read_sizes)


def test_download_failure_removes_part_and_never_exposes_final_file(tmp_path):
    pgn = (FIXTURES / "chess_results_games.pgn").read_bytes()
    page = (FIXTURES / "chess_results_tournament.html").read_bytes()
    responses = {
        PAGE_URL: lambda: FakeResponse(page),
        PGN_URL: lambda: FakeResponse(pgn, chunk_size=3, error_after=3),
    }
    opener = FakeOpener(responses)
    adapter = ChessResultsAdapter(opener=opener)
    ref = adapter.discover(PAGE_URL)[0]
    destination = tmp_path / "games.pgn"

    with pytest.raises(RuntimeError, match="download"):
        adapter.download_pgn(ref, destination)

    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()


def test_http_failure_is_not_reported_as_an_empty_descriptor():
    opener = FakeOpener(
        {PAGE_URL: HTTPError(PAGE_URL, 503, "fixture unavailable", {}, None)}
    )
    adapter = ChessResultsAdapter(opener=opener)
    ref = adapter.discover(PAGE_URL)[0]

    with pytest.raises(RuntimeError, match="HTTP"):
        adapter.describe(ref)


def test_empty_page_is_rejected_as_malformed():
    adapter, _ = _adapter_for_page(b"")
    ref = adapter.discover(PAGE_URL)[0]

    with pytest.raises(RuntimeError, match="malformed"):
        adapter.describe(ref)
