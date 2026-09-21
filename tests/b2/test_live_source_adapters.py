from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import zipfile
from urllib.parse import parse_qs, urlparse

import pytest

from chessgrandmaster.b2.acquisition import AcquisitionService
from chessgrandmaster.b2.cli_acquire import build_adapters, build_parser
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.b2.storage import LocalObjectStore
from chessgrandmaster.b2.sources.base import SourceRef
from chessgrandmaster.b2.sources.chesscom_broadcast import (
    ChessComBroadcastAdapter,
)
from chessgrandmaster.b2.sources.lichess_broadcast import (
    LichessBroadcastAdapter,
)
from chessgrandmaster.b2.sources.twic import TwicAdapter


FIXTURES = Path(__file__).parent / "fixtures"
LICHESS_FEED_URL = "https://lichess.test/api/broadcast/samarkand-2026"
CHESSCOM_FEED_URL = "https://chesscom.test/broadcast/samarkand-2026"
TWIC_ARCHIVE_URL = "https://twic.test/html/twic1660.html"


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        final_url: str | None = None,
        chunk_size: int | None = None,
        error_after: int | None = None,
    ) -> None:
        self._body = BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self.final_url = final_url
        self.chunk_size = chunk_size
        self.error_after = error_after
        self.closed = False
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.error_after is not None and self._body.tell() >= self.error_after:
            raise TimeoutError("fixture stream interrupted")
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
    """Path/method keyed transport; it never performs network I/O."""

    def __init__(self, routes):
        self.routes = routes
        self.request_records: list[dict[str, object]] = []
        self.cookie: str | None = None

    def open(self, request, timeout: float):
        if self.cookie is not None:
            request.add_header("Cookie", self.cookie)
        url = request.full_url
        parsed = urlparse(url)
        method = request.get_method()
        record = {
            "method": method,
            "url": url,
            "path": parsed.path,
            "query": parse_qs(parsed.query, keep_blank_values=True),
            "data": request.data,
            "cookie": request.get_header("Cookie"),
            "timeout": timeout,
        }
        self.request_records.append(record)
        route = self.routes.get((method, parsed.path))
        if route is None:
            route = self.routes.get(parsed.path)
        if route is None:
            raise AssertionError(f"unexpected fixture request: {method} {url}")
        response = route(request) if callable(route) else route
        if isinstance(response, BaseException):
            raise response
        set_cookie = response.headers.get("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        return response


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _response(body: bytes, url: str, **kwargs) -> FakeResponse:
    return FakeResponse(body, final_url=url, **kwargs)


def _lichess_routes(pgn: bytes, *, bad_detail: bool = False, interrupted: bool = False):
    detail = json.loads(_fixture("lichess_broadcast_round.json"))
    if bad_detail:
        detail["event_id"] = "women"
    detail_bytes = json.dumps(detail).encode()

    def pgn_response(request):
        event = parse_qs(urlparse(request.full_url).query).get("event", [""])[0]
        if event != "open":
            raise AssertionError("fixture selected a non-open round")
        return _response(
            pgn,
            request.full_url,
            headers={
                "Content-Type": "text/plain",
                "X-Broadcast-Id": "samarkand-2026",
                "X-Event-Id": "open",
                "X-Round-Id": "round-1",
            },
            chunk_size=3,
            error_after=3 if interrupted else None,
        )

    return {
        ("GET", "/api/broadcast/samarkand-2026"): lambda request: _response(
            _fixture("lichess_broadcast_feed.json"), request.full_url,
            headers={"Set-Cookie": "lichess-session=fixture; Path=/"},
        ),
        ("GET", "/broadcast/samarkand-2026/open/round-1"): lambda request: _response(
            detail_bytes, request.full_url
        ),
        ("GET", "/api/broadcast/round/round-1.pgn"): pgn_response,
    }


def test_lichess_feed_supports_open_and_women_broadcasts_and_filters_online():
    opener = FixtureOpener(_lichess_routes(_fixture("chess_results_games.pgn")))
    adapter = LichessBroadcastAdapter(
        base_url="https://lichess.test", opener=opener
    )

    refs = adapter.discover(LICHESS_FEED_URL)

    assert [ref.external_id for ref in refs] == [
        "broadcast:samarkand-2026/event:open/round:round-1",
        "broadcast:samarkand-2026/event:women/round:round-1",
    ]
    assert all(ref.provider == "lichess-broadcast" for ref in refs)
    assert all("online-practice" not in ref.external_id for ref in refs)
    assert opener.request_records[0]["method"] == "GET"
    assert opener.request_records[0]["path"] == "/api/broadcast/samarkand-2026"


def test_lichess_round_identity_is_validated_before_raw_download(tmp_path):
    opener = FixtureOpener(
        _lichess_routes(_fixture("chess_results_games.pgn"), bad_detail=True)
    )
    adapter = LichessBroadcastAdapter(
        base_url="https://lichess.test", opener=opener
    )
    ref = SourceRef(
        "lichess-broadcast",
        "broadcast:samarkand-2026/event:open/round:round-1",
        "https://lichess.test/broadcast/samarkand-2026/open/round-1",
    )

    with pytest.raises(ValueError, match="event identity"):
        adapter.describe(ref)
    with pytest.raises(ValueError, match="event identity"):
        adapter.download_pgn(ref, tmp_path / "round.pgn")

    assert not (tmp_path / "round.pgn").exists()
    assert not (tmp_path / "round.pgn.part").exists()
    assert all(record["path"] != "/api/broadcast/round/round-1.pgn" for record in opener.request_records)


def test_lichess_download_preserves_raw_bytes_and_reuses_cookie_session(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    opener = FixtureOpener(_lichess_routes(pgn))
    adapter = LichessBroadcastAdapter(
        base_url="https://lichess.test", opener=opener
    )
    ref = adapter.discover(LICHESS_FEED_URL)[0]
    descriptor = adapter.describe(ref)
    destination = tmp_path / "nested" / "round.pgn"

    assert descriptor.is_otb_hint is True
    assert descriptor.event_country_hint == "UZB"
    assert adapter.download_pgn(ref, destination) == destination
    assert destination.read_bytes() == pgn
    assert not (tmp_path / "nested" / "round.pgn.part").exists()
    assert opener.request_records[1]["cookie"] == "lichess-session=fixture"
    assert opener.request_records[2]["cookie"] == "lichess-session=fixture"
    assert opener.request_records[2]["method"] == "GET"
    assert opener.request_records[2]["path"] == "/api/broadcast/round/round-1.pgn"


def test_lichess_rejects_online_or_non_broadcast_queries_without_network():
    adapter = LichessBroadcastAdapter(
        base_url="https://lichess.test", opener=FixtureOpener({})
    )

    for query in (
        "https://lichess.test/api/games/user/alice",
        "https://lichess.test/game/abc123",
        "https://lichess.test/broadcast/samarkand-2026/open",
    ):
        with pytest.raises(ValueError):
            adapter.discover(query)


def test_lichess_interrupted_download_is_atomic(tmp_path):
    opener = FixtureOpener(
        _lichess_routes(_fixture("chess_results_games.pgn"), interrupted=True)
    )
    adapter = LichessBroadcastAdapter(
        base_url="https://lichess.test", opener=opener
    )
    ref = SourceRef(
        "lichess-broadcast",
        "broadcast:samarkand-2026/event:open/round:round-1",
        "https://lichess.test/broadcast/samarkand-2026/open/round-1",
    )
    destination = tmp_path / "round.pgn"

    with pytest.raises(RuntimeError, match="download"):
        adapter.download_pgn(ref, destination)

    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()


def test_lichess_repeated_round_refreshes_are_idempotent_at_acquisition_boundary(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    opener = FixtureOpener(_lichess_routes(pgn))
    adapter = LichessBroadcastAdapter(
        base_url="https://lichess.test", opener=opener
    )
    registry = Registry(tmp_path / "registry.sqlite")
    service = AcquisitionService(
        registry=registry,
        store=LocalObjectStore(tmp_path / "objects"),
        workspace=tmp_path / "workspace",
        adapters={"lichess-broadcast": adapter},
    )
    query = "https://lichess.test/broadcast/samarkand-2026/open/round-1"

    first = service.acquire(query)
    counts_after_first = registry.registry_counts()
    second = service.acquire(query)
    counts_after_second = registry.registry_counts()

    assert first.revision_id == second.revision_id
    assert first.raw_sha256 == second.raw_sha256
    assert counts_after_second["source_files"] == counts_after_first["source_files"] == 1
    assert counts_after_second["canonical_games"] == counts_after_first["canonical_games"]
    assert counts_after_second["game_occurrences"] == counts_after_first["game_occurrences"]
    assert counts_after_second["tournament_revisions"] == counts_after_first["tournament_revisions"]
    assert counts_after_second["download_attempts"] == counts_after_first["download_attempts"] + 1


def _chesscom_routes(pgn: bytes, *, bad_detail: bool = False, html_download: bool = False):
    detail = json.loads(_fixture("chesscom_broadcast_game.json"))
    if bad_detail:
        detail["game_id"] = "another-game"
    detail_bytes = json.dumps(detail).encode()

    return {
        ("GET", "/broadcast/samarkand-2026"): lambda request: _response(
            _fixture("chesscom_broadcast_feed.json"), request.full_url
        ),
        ("GET", "/broadcast/samarkand-2026/games/open-r1-board1"): lambda request: _response(
            detail_bytes, request.full_url
        ),
        ("GET", "/broadcast/samarkand-2026/games/open-r1-board1.pgn"): lambda request: _response(
            b"<html>not a PGN</html>" if html_download else pgn,
            request.full_url,
            headers={"Content-Type": "text/html" if html_download else "text/plain"},
        ),
    }


def test_chesscom_structured_broadcast_feed_requires_otb_event_and_game_identity():
    opener = FixtureOpener(_chesscom_routes(_fixture("chess_results_games.pgn")))
    adapter = ChessComBroadcastAdapter(
        base_url="https://chesscom.test", opener=opener
    )

    refs = adapter.discover(CHESSCOM_FEED_URL)

    assert refs == [
        SourceRef(
            "chesscom-broadcast",
            "event:samarkand-2026/game:open-r1-board1",
            "https://chesscom.test/broadcast/samarkand-2026/games/open-r1-board1",
        )
    ]
    assert opener.request_records[0]["path"] == "/broadcast/samarkand-2026"


def test_chesscom_download_preserves_pgn_bytes_and_source_provenance(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    opener = FixtureOpener(_chesscom_routes(pgn))
    adapter = ChessComBroadcastAdapter(
        base_url="https://chesscom.test", opener=opener
    )
    ref = adapter.discover(CHESSCOM_FEED_URL)[0]
    descriptor = adapter.describe(ref)
    destination = tmp_path / "game.pgn"

    adapter.download_pgn(ref, destination)

    assert descriptor.ref == ref
    assert descriptor.pgn_url == "https://chesscom.test/broadcast/samarkand-2026/games/open-r1-board1.pgn"
    assert descriptor.is_otb_hint is True
    assert destination.read_bytes() == pgn
    assert not Path(f"{destination}.part").exists()
    assert [record["path"] for record in opener.request_records] == [
        "/broadcast/samarkand-2026",
        "/broadcast/samarkand-2026/games/open-r1-board1",
        "/broadcast/samarkand-2026/games/open-r1-board1.pgn",
    ]


def test_chesscom_rejects_online_archive_paths_and_identity_mismatch(tmp_path):
    adapter = ChessComBroadcastAdapter(
        base_url="https://chesscom.test", opener=FixtureOpener({})
    )
    for query in (
        "https://chesscom.test/games/archive/2026/alice",
        "https://chesscom.test/online/alice/game-1",
        "https://chesscom.test/play/computer/game-1",
    ):
        with pytest.raises(ValueError, match="broadcast"):
            adapter.discover(query)

    opener = FixtureOpener(
        _chesscom_routes(_fixture("chess_results_games.pgn"), bad_detail=True)
    )
    adapter = ChessComBroadcastAdapter(
        base_url="https://chesscom.test", opener=opener
    )
    ref = adapter.discover(CHESSCOM_FEED_URL)[0]
    with pytest.raises(ValueError, match="game identity"):
        adapter.describe(ref)
    assert len(opener.request_records) == 2
    assert not (tmp_path / "game.pgn").exists()


def test_chesscom_html_success_body_is_not_installed(tmp_path):
    opener = FixtureOpener(
        _chesscom_routes(_fixture("chess_results_games.pgn"), html_download=True)
    )
    adapter = ChessComBroadcastAdapter(
        base_url="https://chesscom.test", opener=opener
    )
    ref = adapter.discover(CHESSCOM_FEED_URL)[0]
    destination = tmp_path / "game.pgn"

    with pytest.raises(RuntimeError, match="HTML"):
        adapter.download_pgn(ref, destination)

    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()


def test_twic_discovers_archive_pgn_and_preserves_issue_provenance(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    opener = FixtureOpener(
        {
            ("GET", "/html/twic1660.html"): lambda request: _response(
                _fixture("twic1660.html"), request.full_url
            ),
            ("GET", "/assets/files/pgn/twic1660.pgn"): lambda request: _response(
                pgn, request.full_url, headers={"Content-Type": "text/plain"}
            ),
        }
    )
    adapter = TwicAdapter(base_url="https://twic.test", opener=opener)
    ref = adapter.discover("twic1660")[0]
    descriptor = adapter.describe(ref)
    destination = tmp_path / "twic1660.pgn"

    adapter.download_pgn(ref, destination)

    assert ref == SourceRef("twic", "twic1660", TWIC_ARCHIVE_URL)
    assert descriptor.title == "The Week in Chess 1660"
    assert descriptor.pgn_url == "https://twic.test/assets/files/pgn/twic1660.pgn"
    assert destination.read_bytes() == pgn
    assert [record["path"] for record in opener.request_records] == [
        "/html/twic1660.html",
        "/assets/files/pgn/twic1660.pgn",
    ]


@pytest.mark.parametrize(
    "query",
    [
        "twic1659",
        "1659",
        "https://twic.test/html/twic1659.html",
    ],
)
def test_twic_rejects_issues_before_1660(query):
    adapter = TwicAdapter(base_url="https://twic.test", opener=FixtureOpener({}))

    with pytest.raises(ValueError, match="1660"):
        adapter.discover(query)


def test_twic_rejects_issue_mismatch_and_html_download(tmp_path):
    mismatch_opener = FixtureOpener(
        {
            ("GET", "/html/twic1660.html"): lambda request: _response(
                _fixture("twic1659.html"), request.full_url
            )
        }
    )
    adapter = TwicAdapter(base_url="https://twic.test", opener=mismatch_opener)
    ref = adapter.discover("twic1660")[0]
    with pytest.raises(ValueError, match="issue"):
        adapter.describe(ref)

    html_opener = FixtureOpener(
        {
            ("GET", "/assets/files/pgn/twic1660.pgn"): lambda request: _response(
                b"<html><body>blocked</body></html>",
                request.full_url,
                headers={"Content-Type": "text/html"},
            )
        }
    )
    direct = TwicAdapter(base_url="https://twic.test", opener=html_opener)
    direct_ref = direct.discover(
        "https://twic.test/assets/files/pgn/twic1660.pgn"
    )[0]
    destination = tmp_path / "twic1660.pgn"
    with pytest.raises(RuntimeError, match="HTML"):
        direct.download_pgn(direct_ref, destination)
    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()


def test_twic_downloads_pgn_from_a_zip_archive_without_parsing_games(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("twic1660.pgn", pgn)
    archive_bytes = archive.getvalue()
    opener = FixtureOpener(
        {
            ("GET", "/assets/files/cbv/twic1660.zip"): lambda request: _response(
                archive_bytes,
                request.full_url,
                headers={"Content-Type": "application/zip"},
                chunk_size=5,
            )
        }
    )
    adapter = TwicAdapter(base_url="https://twic.test", opener=opener)
    ref = adapter.discover(
        "https://twic.test/assets/files/cbv/twic1660.zip"
    )[0]
    destination = tmp_path / "twic1660.pgn"

    adapter.download_pgn(ref, destination)

    assert destination.read_bytes() == pgn
    assert not Path(f"{destination}.part").exists()
    assert not Path(f"{destination}.archive.part").exists()


def test_adapter_registry_is_additive_and_help_names_all_providers():
    adapters = build_adapters()

    assert set(adapters) == {
        "chess-results",
        "lichess-broadcast",
        "chesscom-broadcast",
        "twic",
    }
    assert isinstance(adapters["lichess-broadcast"], LichessBroadcastAdapter)
    assert isinstance(adapters["chesscom-broadcast"], ChessComBroadcastAdapter)
    assert isinstance(adapters["twic"], TwicAdapter)
    help_text = build_parser().format_help()
    for name in ("lichess-broadcast", "chesscom-broadcast", "twic"):
        assert name in help_text
