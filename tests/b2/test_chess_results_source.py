from __future__ import annotations

from dataclasses import fields
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPCookieProcessor

import pytest

from chessgrandmaster.b2.sources.base import SourceDescriptor, SourceRef
from chessgrandmaster.b2.sources.chess_results import (
    ChessResultsAdapter,
    _available_rounds,
    _download_form,
    _parse_page,
    database_key_from_ref,
)


FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_URL = (
    "https://s3.chess-results.com/tnr1450909.aspx"
    "?lan=1&art=0&turdet=YES&SNode=S0"
)
SEARCH_URL = "https://s3.chess-results.com/partiesuche.aspx"
CUSTOM_SEARCH_URL = f"{SEARCH_URL}?lang=1"
GET_DOWNLOAD_URL = "https://s3.chess-results.com/download/fixture-get"
GET_DOWNLOAD_REQUEST_URL = (
    f"{GET_DOWNLOAD_URL}?"
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
POST_DOWNLOAD_URL = "https://s3.chess-results.com/download/fixture-post"


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        chunk_size: int | None = None,
        error_after: int | None = None,
        headers: dict[str, str] | None = None,
        final_url: str | None = None,
    ):
        self._body = BytesIO(body)
        self.status = status
        self.chunk_size = chunk_size
        self.error_after = error_after
        self.headers = headers or {}
        self.final_url = final_url
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

    def geturl(self) -> str:
        return self.final_url or ""

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """Fixture-only transport; an unexpected URL is a test failure."""

    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, float, str | None]] = []
        self.opened_responses: list[tuple[str, FakeResponse]] = []
        self.request_records: list[dict[str, object]] = []

    def open(self, request, timeout):
        url = request.full_url
        method = request.get_method()
        self.calls.append((url, timeout, request.get_header("User-agent")))
        self.request_records.append(
            {
                "url": url,
                "method": method,
                "data": request.data,
                "cookie": request.get_header("Cookie"),
                "user_agent": request.get_header("User-agent"),
            }
        )
        response = self.responses.get((url, method))
        if response is None:
            response = self.responses.get(url)
        if response is None:
            raise AssertionError(f"unexpected fixture request: {method} {url}")
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        self.opened_responses.append((url, response))
        return response


class CookieSessionFakeOpener(FakeOpener):
    """Small fake session that models Set-Cookie reuse without networking."""

    def __init__(self, responses):
        super().__init__(responses)
        self.cookie: str | None = None

    def open(self, request, timeout):
        if self.cookie is not None:
            request.add_header("Cookie", self.cookie)
        response = super().open(request, timeout)
        set_cookie = response.headers.get("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        return response


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _workflow(
    *,
    search_fixture: str = "chess_results_partiesuche.html",
    result_fixture: str = "chess_results_database_get.html",
    download_response: FakeResponse | None = None,
    custom_search: bool = False,
    opener_type=FakeOpener,
):
    search_url = SEARCH_URL
    search_post_url = CUSTOM_SEARCH_URL if custom_search else SEARCH_URL
    download_url = (
        POST_DOWNLOAD_URL if result_fixture.endswith("_post.html") else GET_DOWNLOAD_REQUEST_URL
    )
    responses = {
        (search_url, "GET"): lambda: FakeResponse(
            _fixture(search_fixture),
            final_url=search_url,
        ),
        (search_post_url, "POST"): lambda: FakeResponse(
            _fixture(result_fixture),
            final_url=search_post_url,
        ),
        (download_url, "GET"): lambda: download_response or FakeResponse(
            _fixture("chess_results_games.pgn"),
            chunk_size=3,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="fixture.pgn"',
            },
            final_url=download_url,
        ),
        (download_url, "POST"): lambda: download_response or FakeResponse(
            _fixture("chess_results_games.pgn"),
            chunk_size=3,
            headers={"Content-Type": "text/plain"},
            final_url=download_url,
        ),
    }
    opener = opener_type(responses)
    return ChessResultsAdapter(timeout_sec=7.5, opener=opener), opener


def _request_params(record: dict[str, object]) -> dict[str, list[str]]:
    data = record["data"]
    if not isinstance(data, bytes):
        raise AssertionError("expected a form body")
    return parse_qs(data.decode(), keep_blank_values=True)


def test_source_descriptor_does_not_grow_a_chess_results_games_url_field():
    assert "games_url" not in {field.name for field in fields(SourceDescriptor)}


def test_discover_literal_normalizes_to_one_source_ref():
    adapter = ChessResultsAdapter(opener=FakeOpener({}))

    assert adapter.discover("tnr1450909") == [
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
    adapter = ChessResultsAdapter(opener=FakeOpener({}))

    ref = adapter.discover(source_url)[0]

    assert ref == SourceRef("chess-results", "tnr1450909", source_url)


def test_discover_rejects_unrelated_domains_and_free_text():
    adapter = ChessResultsAdapter(opener=FakeOpener({}))

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


def test_database_key_helper_returns_digits_without_navigation():
    ref = SourceRef("chess-results", "tnr1450909", SOURCE_URL)

    assert database_key_from_ref(ref) == "1450909"


@pytest.mark.parametrize(
    "ref",
    [
        SourceRef("chess-results", "tnr", SOURCE_URL),
        SourceRef("chess-results", "tnr14x", SOURCE_URL),
        SourceRef("other", "tnr1450909", SOURCE_URL),
    ],
)
def test_database_key_helper_rejects_malformed_external_ids(ref):
    with pytest.raises(ValueError):
        database_key_from_ref(ref)


def test_describe_reads_metadata_without_requiring_a_games_link():
    page = b"<html><h2>Old tournament page</h2><p>Metadata only</p></html>"
    opener = FakeOpener(
        {SOURCE_URL: lambda: FakeResponse(page, final_url=SOURCE_URL)}
    )
    adapter = ChessResultsAdapter(opener=opener)
    ref = adapter.discover(SOURCE_URL)[0]

    descriptor = adapter.describe(ref)

    assert descriptor.title == "Old tournament page"
    assert descriptor.pgn_url is None
    assert len(opener.request_records) == 1


def test_describe_extracts_same_host_direct_pgn_metadata_when_exposed():
    pgn_url = "https://s3.chess-results.com/static/games.pgn"
    page = (
        "<html><h1>Direct fixture</h1>"
        f'<a href="{pgn_url}">PGN</a></html>'
    ).encode()
    opener = FakeOpener(
        {SOURCE_URL: lambda: FakeResponse(page, final_url=SOURCE_URL)}
    )
    adapter = ChessResultsAdapter(opener=opener)
    ref = adapter.discover(SOURCE_URL)[0]

    assert adapter.describe(ref).pgn_url == pgn_url


def test_fixture_parser_finds_actual_search_form_and_key_control():
    parser = _parse_page(_fixture("chess_results_partiesuche.html").decode())

    assert len(parser.forms) == 1
    form = parser.forms[0]
    assert form.method == "POST"
    assert form.action == "/partiesuche.aspx"
    assert [control.name for control in form.submit_controls] == [
        "ctl00$P1$cb_SuchenPartie",
        "ctl00$P1$cb_DownLoadPGN",
    ]
    assert any(
        control.name == "ctl00$P1$txt_dbkey"
        and control.input_type == "text"
        for control in form.controls
    )


def test_download_uses_search_form_field_name_hidden_controls_and_no_50023(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    adapter, opener = _workflow(
        download_response=FakeResponse(
            pgn,
            chunk_size=3,
            headers={"Content-Type": "text/plain"},
            final_url=POST_DOWNLOAD_URL,
        ),
        result_fixture="chess_results_database_post.html",
    )
    ref = adapter.discover(SOURCE_URL)[0]

    adapter.download_pgn(ref, tmp_path / "games.pgn")

    assert [record["url"] for record in opener.request_records] == [
        SEARCH_URL,
        SEARCH_URL,
        POST_DOWNLOAD_URL,
    ]
    search_request = opener.request_records[1]
    assert search_request["method"] == "POST"
    params = _request_params(search_request)
    assert params["ctl00$P1$txt_dbkey"] == ["1450909"]
    assert params["__VIEWSTATE"] == ["fixture-viewstate"]
    assert params["__EVENTVALIDATION"] == ["fixture-eventvalidation"]
    assert params["ctl00$P1$cb_SuchenPartie"] == ["Search"]
    assert all("50023" not in str(record["url"]) for record in opener.request_records)
    assert all(SOURCE_URL != record["url"] for record in opener.request_records)


def test_database_result_is_rejected_when_no_row_matches_requested_key(tmp_path):
    adapter, opener = _workflow(
        result_fixture="chess_results_database_wrong.html",
    )
    ref = adapter.discover(SOURCE_URL)[0]
    destination = tmp_path / "games.pgn"

    with pytest.raises(RuntimeError, match="database key 1450909"):
        adapter.download_pgn(ref, destination)

    assert len(opener.request_records) == 2
    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()


def test_result_rounds_and_game_counts_are_parsed_from_game_rows():
    parser = _parse_page(_fixture("chess_results_database_get.html").decode())

    assert _available_rounds(parser) == (
        (1, 2),
        (3, 1),
        (7, 3),
    )


def test_download_form_is_selected_from_the_actual_pgn_submit_control():
    parser = _parse_page(_fixture("chess_results_database_get.html").decode())

    form, submit = _download_form(parser)

    assert form.method == "GET"
    assert form.action == "/download/fixture-get?format=pgn"
    assert submit.name == "download_action"
    assert submit.value == "Download as PGN-File"


def test_get_download_selects_minimum_and_maximum_available_rounds_atomically(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    adapter, opener = _workflow(
        download_response=FakeResponse(
            pgn,
            chunk_size=3,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="fixture.pgn"',
            },
            final_url=GET_DOWNLOAD_REQUEST_URL,
        )
    )
    ref = adapter.discover(SOURCE_URL)[0]
    destination = tmp_path / "nested" / "games.pgn"

    assert adapter.download_pgn(ref, destination) == destination

    assert destination.read_bytes() == pgn
    assert not Path(f"{destination}.part").exists()
    download_request = opener.request_records[2]
    assert download_request["method"] == "GET"
    params = parse_qs(urlparse(str(download_request["url"])).query)
    assert params["database_key"] == ["1450909"]
    assert params["round_from"] == ["1"]
    assert params["round_to"] == ["7"]
    assert params["download_action"] == ["Download as PGN-File"]
    download_response = opener.opened_responses[2][1]
    assert download_response.read_sizes
    assert all(size == 1024 * 1024 for size in download_response.read_sizes)


def test_post_download_supports_custom_form_and_round_field_names(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    adapter, opener = _workflow(
        search_fixture="chess_results_partiesuche_custom.html",
        result_fixture="chess_results_database_post.html",
        custom_search=True,
        download_response=FakeResponse(
            pgn,
            chunk_size=3,
            headers={"Content-Type": "text/plain"},
            final_url=POST_DOWNLOAD_URL,
        ),
    )
    ref = adapter.discover(SOURCE_URL)[0]
    destination = tmp_path / "post" / "games.pgn"

    adapter.download_pgn(ref, destination)

    assert destination.read_bytes() == pgn
    search_params = _request_params(opener.request_records[1])
    assert search_params["custom$database_key"] == ["1450909"]
    download_request = opener.request_records[2]
    assert download_request["url"] == POST_DOWNLOAD_URL
    assert download_request["method"] == "POST"
    params = _request_params(download_request)
    assert params["search_token"] == ["post-token"]
    assert params["database_key"] == ["1450909"]
    assert params["first_round"] == ["2"]
    assert params["last_round"] == ["9"]
    assert params["download_action"] == ["pgn"]


def test_download_reuses_one_cookie_session_across_search_and_download(tmp_path):
    adapter, opener = _workflow(opener_type=CookieSessionFakeOpener)
    opener.responses[(SEARCH_URL, "GET")] = lambda: FakeResponse(
        _fixture("chess_results_partiesuche.html"),
        headers={"Set-Cookie": "fixture-session=one; Path=/"},
        final_url=SEARCH_URL,
    )
    ref = adapter.discover(SOURCE_URL)[0]

    adapter.download_pgn(ref, tmp_path / "games.pgn")

    assert opener.request_records[0]["cookie"] is None
    assert all(
        record["cookie"] == "fixture-session=one"
        for record in opener.request_records[1:]
    )


def test_default_opener_has_a_cookie_jar_for_real_session_reuse():
    adapter = ChessResultsAdapter()

    assert adapter.cookie_jar is not None
    assert any(
        isinstance(processor, HTTPCookieProcessor)
        for processor in adapter.opener.handlers
    )


def test_tournament_page_is_not_fetched_by_download_workflow(tmp_path):
    adapter, opener = _workflow()
    ref = adapter.discover(SOURCE_URL)[0]

    adapter.download_pgn(ref, tmp_path / "games.pgn")

    assert all(record["url"] != SOURCE_URL for record in opener.request_records)


def test_cross_origin_search_form_is_rejected_before_following_it(tmp_path):
    unsafe_search = (
        b"<html><form action='https://evil.example/search' method='post'>"
        b"<input name='database_key' type='text'>"
        b"<input name='search' type='submit' value='Search'></form></html>"
    )
    adapter, opener = _workflow()
    opener.responses[(SEARCH_URL, "GET")] = lambda: FakeResponse(
        unsafe_search,
        final_url=SEARCH_URL,
    )
    ref = adapter.discover(SOURCE_URL)[0]

    with pytest.raises(RuntimeError, match="approved source hosts"):
        adapter.download_pgn(ref, tmp_path / "games.pgn")

    assert len(opener.request_records) == 1


def test_cross_origin_final_response_is_rejected():
    opener = FakeOpener(
        {
            SOURCE_URL: lambda: FakeResponse(
                b"<html><h1>Redirected</h1></html>",
                final_url="https://evil.example/tnr1450909.aspx",
            )
        }
    )
    adapter = ChessResultsAdapter(opener=opener)
    ref = adapter.discover(SOURCE_URL)[0]

    with pytest.raises(RuntimeError, match="approved source hosts"):
        adapter.describe(ref)


def test_http_failure_is_not_reported_as_an_empty_descriptor():
    opener = FakeOpener(
        {SOURCE_URL: HTTPError(SOURCE_URL, 503, "fixture unavailable", {}, None)}
    )
    adapter = ChessResultsAdapter(opener=opener)
    ref = adapter.discover(SOURCE_URL)[0]

    with pytest.raises(RuntimeError, match="HTTP"):
        adapter.describe(ref)


def test_zero_length_download_is_rejected_before_install(tmp_path):
    adapter, _ = _workflow(
        download_response=FakeResponse(
            b"",
            headers={"Content-Type": "application/octet-stream"},
            final_url=GET_DOWNLOAD_REQUEST_URL,
        )
    )
    ref = adapter.discover(SOURCE_URL)[0]
    destination = tmp_path / "games.pgn"

    with pytest.raises(RuntimeError, match="empty"):
        adapter.download_pgn(ref, destination)

    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()


def test_html_error_download_is_rejected_before_install(tmp_path):
    adapter, _ = _workflow(
        download_response=FakeResponse(
            b"<html><body>search error</body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
            final_url=GET_DOWNLOAD_REQUEST_URL,
        )
    )
    ref = adapter.discover(SOURCE_URL)[0]
    destination = tmp_path / "games.pgn"

    with pytest.raises(RuntimeError, match="HTML"):
        adapter.download_pgn(ref, destination)

    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()


def test_partial_download_leaves_no_final_file_or_part_file(tmp_path):
    pgn = _fixture("chess_results_games.pgn")
    adapter, _ = _workflow(
        download_response=FakeResponse(
            pgn,
            chunk_size=3,
            error_after=3,
            headers={"Content-Type": "text/plain"},
            final_url=GET_DOWNLOAD_REQUEST_URL,
        )
    )
    ref = adapter.discover(SOURCE_URL)[0]
    destination = tmp_path / "games.pgn"

    with pytest.raises(RuntimeError, match="download"):
        adapter.download_pgn(ref, destination)

    assert not destination.exists()
    assert not Path(f"{destination}.part").exists()


def test_fixture_download_uses_only_injected_transport_and_never_network(tmp_path):
    adapter, opener = _workflow()
    ref = adapter.discover(SOURCE_URL)[0]

    adapter.download_pgn(ref, tmp_path / "games.pgn")

    assert adapter.opener is opener
    assert len(opener.request_records) == 3