from __future__ import annotations

from http.client import InvalidURL
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
    ):
        with pytest.raises(ValueError):
            adapter.discover(query)


def test_lichess_html_root_and_event_feed_enumerate_round_and_download_pgn(tmp_path):
    root_url = "https://lichess.test/broadcast/olympiad-2026/root-id"
    round_url = "https://lichess.test/broadcast/olympiad-open/round-1/open-round-id"
    pgn = _fixture("chess_results_games.pgn")
    opener = FixtureOpener(
        {
            ("GET", "/broadcast/olympiad-2026/root-id"): lambda request: _response(
                _fixture("lichess_broadcast_root.html"), request.full_url
            ),
            ("GET", "/broadcast/olympiad-open/open-event-id"): lambda request: _response(
                _fixture("lichess_broadcast_event.html"), request.full_url
            ),
            ("GET", "/broadcast/olympiad-open/round-1/open-round-id"): lambda request: _response(
                _fixture("lichess_broadcast_round.html"), request.full_url
            ),
            ("GET", "/api/broadcast/round/open-round-id.pgn"): lambda request: _response(
                pgn, request.full_url, headers={"Content-Type": "text/plain"}
            ),
        }
    )
    adapter = LichessBroadcastAdapter(base_url="https://lichess.test", opener=opener)

    refs = adapter.discover(root_url)
    assert refs == [
        SourceRef("lichess-broadcast", "broadcast:olympiad-open/event:round-1/round:open-round-id", round_url)
    ]
    descriptor = adapter.describe(refs[0])
    destination = tmp_path / "round.pgn"
    adapter.download_pgn(refs[0], destination)
    assert descriptor.is_otb_hint is True
    assert destination.read_bytes() == pgn
    assert [record["path"] for record in opener.request_records] == [
        "/broadcast/olympiad-2026/root-id",
        "/broadcast/olympiad-open/open-event-id",
        "/broadcast/olympiad-open/round-1/open-round-id",
        "/api/broadcast/round/open-round-id.pgn",
    ]


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


def test_chesscom_event_page_uses_live_room_and_game_api_for_otb_pgn(tmp_path):
    event_url = "https://chesscom.test/events/2026-fide-chess-olympiad-open/games"
    game_url = (
        "https://chesscom.test/events/2026-fide-chess-olympiad-open/01/"
        "Otsuka_Shou-Alfene_Ambdullah"
    )
    opener = FixtureOpener(
        {
            ("GET", "/events/2026-fide-chess-olympiad-open/games"): lambda request: _response(
                b"<html><body>event page</body></html>", request.full_url
            ),
            ("POST", "/events/v1/api/room/2026-fide-chess-olympiad-open"): lambda request: _response(
                _fixture("chesscom_room.json"), request.full_url,
                headers={"Content-Type": "application/json"},
            ),
            ("GET", "/events/2026-fide-chess-olympiad-open/01/Otsuka_Shou-Alfene_Ambdullah"): lambda request: _response(
                b"<html><head><title>Olympiad OTB game</title></head><body>broadcast game</body></html>",
                request.full_url,
            ),
            ("POST", "/events/v1/api/game/2026-fide-chess-olympiad-open/01/Otsuka_Shou-Alfene_Ambdullah"): lambda request: _response(
                _fixture("chesscom_game.json"), request.full_url,
                headers={"Content-Type": "application/json"},
            ),
        }
    )
    adapter = ChessComBroadcastAdapter(base_url="https://chesscom.test", opener=opener)

    refs = adapter.discover(event_url)
    assert refs == [SourceRef("chesscom-broadcast", "event:2026-fide-chess-olympiad-open/game:Otsuka_Shou-Alfene_Ambdullah", game_url)]
    descriptor = adapter.describe(refs[0])
    destination = tmp_path / "game.pgn"
    adapter.download_pgn(refs[0], destination)

    assert descriptor.is_otb_hint is True
    assert descriptor.pgn_url.endswith("event=2026-fide-chess-olympiad-open&game=Otsuka_Shou-Alfene_Ambdullah")
    assert [(record["method"], record["path"]) for record in opener.request_records] == [
        ("GET", "/events/2026-fide-chess-olympiad-open/games"),
        ("POST", "/events/v1/api/room/2026-fide-chess-olympiad-open"),
        ("GET", "/events/2026-fide-chess-olympiad-open/01/Otsuka_Shou-Alfene_Ambdullah"),
        ("POST", "/events/v1/api/game/2026-fide-chess-olympiad-open/01/Otsuka_Shou-Alfene_Ambdullah"),
    ]
    assert b'[White "Otsuka, Shou"]' in destination.read_bytes()
    assert b"1. e4 c5 2. Nf3 Nc6 1-0" in destination.read_bytes()


def test_chesscom_provider_source_url_percent_encodes_spaces_before_http(tmp_path):
    raw_path = "/events/2026-fide-chess-olympiad-open/Samarkand, UZ"
    encoded_path = "/events/2026-fide-chess-olympiad-open/Samarkand,%20UZ"
    payload = {
        "event_id": "2026-fide-chess-olympiad-open",
        "games": [
            {
                "game_id": "board-1",
                "source_url": f"https://chesscom.test{raw_path}",
                "otb": True,
                "broadcast": True,
            }
        ],
    }

    def page_response(request):
        if any(character.isspace() for character in request.full_url):
            raise InvalidURL(f"URL contains raw whitespace: {request.full_url!r}")
        return _response(
            b"<html><head><title>Olympiad OTB game</title></head>"
            b"<body>broadcast game</body></html>",
            request.full_url,
        )

    opener = FixtureOpener(
        {
            ("GET", "/broadcast/samarkand-2026"): lambda request: _response(
                json.dumps(payload).encode(), request.full_url
            ),
            ("GET", raw_path): page_response,
            ("GET", encoded_path): page_response,
        }
    )
    adapter = ChessComBroadcastAdapter(base_url="https://chesscom.test", opener=opener)

    discovered_ref = adapter.discover(CHESSCOM_FEED_URL)[0]
    assert discovered_ref.source_url == f"https://chesscom.test{encoded_path}"
    raw_ref = SourceRef(
        "chesscom-broadcast",
        "event:2026-fide-chess-olympiad-open/game:board-1",
        f"https://chesscom.test{raw_path}",
    )
    descriptor = adapter.describe(raw_ref)

    assert descriptor.ref.source_url == f"https://chesscom.test{encoded_path}"
    assert opener.request_records[-1]["path"] == encoded_path


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
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("twic1660.pgn", pgn)
    archive_bytes = archive.getvalue()
    opener = FixtureOpener(
        {
            ("GET", "/html/twic1660.html"): lambda request: _response(
                _fixture("twic1660.html"), request.full_url
            ),
            ("GET", "/zips/twic1660g.zip"): lambda request: _response(
                archive_bytes, request.full_url, headers={"Content-Type": "application/zip"}
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
    assert descriptor.pgn_url == "https://twic.test/zips/twic1660g.zip"
    assert destination.read_bytes() == pgn
    assert [record["path"] for record in opener.request_records] == [
        "/html/twic1660.html",
        "/zips/twic1660g.zip",
    ]


def test_twic_retries_deterministic_zip_once_after_http_406(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    archive = BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("twic1660.pgn", pgn)
    archive_bytes = archive.getvalue()
    zip_attempts = 0

    def zip_response(request):
        nonlocal zip_attempts
        zip_attempts += 1
        if zip_attempts == 1:
            return _response(b"", request.full_url, status=406)
        return _response(
            archive_bytes,
            request.full_url,
            headers={"Content-Type": "application/zip"},
        )

    opener = FixtureOpener(
        {
            ("GET", "/html/twic1660.html"): lambda request: _response(
                _fixture("twic1660.html"), request.full_url
            ),
            ("GET", "/zips/twic1660g.zip"): zip_response,
        }
    )
    adapter = TwicAdapter(base_url="https://twic.test", opener=opener)
    ref = adapter.discover("twic1660")[0]
    destination = tmp_path / "twic1660.pgn"

    adapter.download_pgn(ref, destination)

    assert destination.read_bytes() == pgn
    assert zip_attempts == 2
    assert [record["path"] for record in opener.request_records] == [
        "/html/twic1660.html",
        "/zips/twic1660g.zip",
        "/html/twic1660.html",
        "/zips/twic1660g.zip",
    ]


def test_twic_describe_keeps_deterministic_zip_when_metadata_times_out():
    opener = FixtureOpener(
        {
            ("GET", "/html/twic1660.html"): RuntimeError("metadata timed out"),
        }
    )
    adapter = TwicAdapter(base_url="https://twic.test", opener=opener)
    ref = adapter.discover("twic1660")[0]

    descriptor = adapter.describe(ref)

    assert descriptor.pgn_url == "https://twic.test/zips/twic1660g.zip"
    assert descriptor.title == "The Week in Chess 1660"


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
            ("GET", "/zips/twic1660g.zip"): lambda request: _response(
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
            ("GET", "/zips/twic1660g.zip"): lambda request: _response(
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
