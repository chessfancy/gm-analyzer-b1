from __future__ import annotations

from urllib.parse import parse_qs

from chessgrandmaster.discovery.chess_results import (
    ChessResultsCandidate,
    ChessResultsDiscovery,
    DiscoveryResult,
    PlayerEvidence,
    _find_player_form,
    _parse_discovery_page,
    merge_corpus_priority,
)


CORPUS_FORM_HTML = """
<!doctype html>
<html><body>
  <form action="/opaque-corpus-submit" method="post">
    <input type="hidden" name="csrf-state" value="fixture-state">
    <label for="date-from-control">Tournament end between from</label>
    <input id="date-from-control" name="x_date_a" type="date">
    <label for="date-to-control">Tournament end between to</label>
    <input id="date-to-control" name="x_date_b" type="date">
    <label for="standard-control">Time control</label>
    <select id="standard-control" name="x_time_mode">
      <option value="mode-any">Any</option>
      <option value="mode-standard">Standard / Classical</option>
      <option value="mode-rapid">Rapid</option>
      <option value="mode-blitz">Blitz</option>
    </select>
    <label for="finished-control">Only finished tournaments</label>
    <input id="finished-control" name="x_finished" type="checkbox" value="yes">
    <label for="games-control">Games available</label>
    <input id="games-control" name="x_games" type="checkbox" value="yes">
    <label for="lines-control">Maximum number of lines</label>
    <select id="lines-control" name="x_lines">
      <option value="line-100">100</option>
      <option value="line-2000">2000</option>
    </select>
    <label for="sort-control">Sort according to</label>
    <select id="sort-control" name="x_sort">
      <option value="sort-name">Name</option>
      <option value="sort-update">last update</option>
    </select>
    <label for="country-control">Country</label>
    <select id="country-control" name="x_country">
      <option value="all-countries">All countries</option>
      <option value="VIE">Vietnam</option>
    </select>
    <input id="database-key-control" name="x_db" type="text">
    <input id="fide-event-control" name="x_event_id" type="text">
    <input id="tournament-control" name="x_tournament" type="text">
    <button name="x_submit" value="run-search">Search</button>
  </form>
</body></html>
"""


CORPUS_RESULT_HTML = """
<html><body>
  <table>
    <tr>
      <th>No.</th><th>Tournament</th><th>FED</th><th>from</th><th>to</th>
      <th>Time control</th><th>n</th><th>dbkey</th><th>EventID</th>
    </tr>
    <tr>
      <td>1</td><td><a href="/tnr1001.aspx">VIE Standard Open</a></td>
      <td>VIE</td><td>2026/01/02</td><td>2026/01/04</td>
      <td>90 min + 30 sec</td><td>42</td><td>1001</td><td>9001</td>
    </tr>
    <tr>
      <td>2</td><td><a href="/tnr1002.aspx">Foreign Classical Open</a></td>
      <td>THA</td><td>2026/01/10</td><td>2026/01/12</td>
      <td>Standard / Classical</td><td>18</td><td>1002</td><td>9002</td>
    </tr>
    <tr>
      <td>3</td><td><a href="/tnr1003.aspx">Rapid Excluded</a></td>
      <td>GER</td><td>2026/01/10</td><td>2026/01/12</td>
      <td>Rapid</td><td>20</td><td>1003</td><td>9003</td>
    </tr>
    <tr>
      <td>4</td><td><a href="/tnr1004.aspx">No Games Excluded</a></td>
      <td>ARG</td><td>2026/01/10</td><td>2026/01/12</td>
      <td>Standard</td><td>0</td><td>1004</td><td>9004</td>
    </tr>
  </table>
</body></html>
"""


PLAYER_FORM_WITH_FIDE_SORT_HTML = """
<html><body>
  <form action="/player-search" method="post">
    <input name="opaque-fide-input" type="text" aria-label="FIDE-ID *)">
    <select name="opaque-sort" aria-label="Sort according to">
      <option value="0">Playername</option>
      <option value="2">FIDE-Id, Enddate of tournament descending</option>
    </select>
    <button name="opaque-submit" value="go">Search</button>
  </form>
</body></html>
"""


def test_player_form_ignores_fide_id_sort_option_when_finding_input():
    form, fide_control, submit = _find_player_form(
        _parse_discovery_page(PLAYER_FORM_WITH_FIDE_SORT_HTML.encode("utf-8"))
    )

    assert form.method == "POST"
    assert fide_control.name == "opaque-fide-input"
    assert submit.name == "opaque-submit"


class _Response:
    status = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __init__(self, body: str, url: str) -> None:
        self._body = body.encode("utf-8")
        self._url = url

    def read(self, _size: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        pass


class _CorpusOpener:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, list[str]]]] = []

    def open(self, request, timeout: float):
        if request.method == "GET":
            fields = {}
            body = CORPUS_FORM_HTML
        else:
            fields = parse_qs((request.data or b"").decode("utf-8"), keep_blank_values=True)
            body = CORPUS_RESULT_HTML
        self.calls.append((request.method, request.full_url, fields))
        return _Response(body, request.full_url)


def test_corpus_semantically_submits_all_standard_download_filters():
    opener = _CorpusOpener()
    result = ChessResultsDiscovery(opener=opener).discover_corpus(
        year=2026,
        max_lines=2000,
    )

    assert [candidate.external_id for candidate in result.candidates] == [
        "tnr1001",
        "tnr1002",
    ]
    assert result.candidates[0].event_country == "VIE"
    assert result.candidates[0].from_date == "2026-01-02"
    assert result.candidates[0].to_date == "2026-01-04"
    assert result.candidates[0].database_key == "1001"
    assert result.candidates[0].provider_game_count == 42
    assert result.candidates[0].time_control_hint == "standard"

    post_calls = [call for call in opener.calls if call[0] == "POST"]
    assert len(post_calls) == 12
    fields = post_calls[0][2]
    assert fields["csrf-state"] == ["fixture-state"]
    assert fields["x_date_a"] == ["2026-01-01"]
    assert fields["x_date_b"] == ["2026-01-31"]
    assert fields["x_time_mode"] == ["mode-standard"]
    assert fields["x_finished"] == ["yes"]
    assert fields["x_games"] == ["yes"]
    assert fields["x_lines"] == ["line-2000"]
    assert fields["x_sort"] == ["sort-update"]
    assert fields["x_country"] == ["all-countries"]
    assert fields["x_submit"] == ["run-search"]


def test_corpus_year_partition_has_all_calendar_months_and_leap_safe_boundaries():
    opener = _CorpusOpener()
    result = ChessResultsDiscovery(opener=opener).discover_corpus(year=2026)

    assert [(window.from_date, window.to_date) for window in result.windows] == [
        ("2026-01-01", "2026-01-31"),
        ("2026-02-01", "2026-02-28"),
        ("2026-03-01", "2026-03-31"),
        ("2026-04-01", "2026-04-30"),
        ("2026-05-01", "2026-05-31"),
        ("2026-06-01", "2026-06-30"),
        ("2026-07-01", "2026-07-31"),
        ("2026-08-01", "2026-08-31"),
        ("2026-09-01", "2026-09-30"),
        ("2026-10-01", "2026-10-31"),
        ("2026-11-01", "2026-11-30"),
        ("2026-12-01", "2026-12-31"),
    ]
    assert all(window.returned_row_count == 2 for window in result.windows)


def _result_for_rows(rows: list[tuple[str, str, str, str, str, int, str]]) -> str:
    body = [
        "<html><body><table>",
        "<tr><th>No.</th><th>Tournament</th><th>FED</th><th>from</th>"
        "<th>to</th><th>Time control</th><th>n</th><th>dbkey</th>"
        "<th>EventID</th></tr>",
    ]
    for index, (title, fed, start, end, time_control, games, key) in enumerate(rows, 1):
        body.append(
            f'<tr><td>{index}</td><td><a href="/tnr{key}.aspx">{title}</a></td>'
            f"<td>{fed}</td><td>{start}</td><td>{end}</td>"
            f"<td>{time_control}</td><td>{games}</td><td>{key}</td><td>0</td></tr>"
        )
    body.append("</table></body></html>")
    return "".join(body)


class _WindowOpener:
    def __init__(self, row_factory):
        self.row_factory = row_factory
        self.windows: list[tuple[str, str]] = []

    def open(self, request, timeout: float):
        if request.method == "GET":
            body = CORPUS_FORM_HTML
        else:
            fields = parse_qs(
                (request.data or b"").decode("utf-8"),
                keep_blank_values=True,
            )
            from_date = fields["x_date_a"][0]
            to_date = fields["x_date_b"][0]
            self.windows.append((from_date, to_date))
            body = _result_for_rows(self.row_factory(from_date, to_date))
        return _Response(body, request.full_url)


def _window_row(
    key: int,
    *,
    start: str = "2026/01/01",
    end: str = "2026/01/01",
) -> tuple[str, str, str, str, str, int, str]:
    return (
        f"Tournament {key}",
        "GER",
        start,
        end,
        "90 min + 30 sec",
        1,
        str(key),
    )


def test_saturated_month_splits_and_dedupes_children_without_using_limit_as_scan_stop():
    def rows_for_window(from_date: str, to_date: str):
        if (from_date, to_date) == ("2026-01-01", "2026-01-31"):
            return [_window_row(200000 + index) for index in range(2000)]
        if (from_date, to_date) == ("2026-01-01", "2026-01-16"):
            return [_window_row(10000 + index) for index in range(1200)]
        if (from_date, to_date) == ("2026-01-17", "2026-01-31"):
            return [_window_row(11000 + index) for index in range(900)]
        return []

    opener = _WindowOpener(rows_for_window)
    result = ChessResultsDiscovery(opener=opener).discover_corpus(
        year=2026,
        max_lines=2000,
        limit=3,
    )

    january = [window for window in result.windows if window.from_date.startswith("2026-01")]
    assert january[0] == type(january[0])(
        "2026-01-01",
        "2026-01-31",
        2000,
        split=True,
        saturated=False,
    )
    assert {(window.from_date, window.to_date, window.returned_row_count) for window in january[1:]} == {
        ("2026-01-01", "2026-01-16", 1200),
        ("2026-01-17", "2026-01-31", 900),
    }
    assert len(result.candidates) == 3
    assert len({candidate.external_id for candidate in result.candidates}) == 3
    assert result.saturated_windows == ()
    assert ("2026-01-01", "2026-01-16") in opener.windows
    assert ("2026-01-17", "2026-01-31") in opener.windows


def test_recursive_saturation_reaches_one_day_and_reports_structured_warning():
    def rows_for_window(from_date: str, to_date: str):
        if from_date.startswith("2026-01"):
            return [_window_row(300000 + index) for index in range(2000)]
        return []

    result = ChessResultsDiscovery(opener=_WindowOpener(rows_for_window)).discover_corpus(
        year=2026,
        max_lines=2000,
    )

    assert result.saturated_windows
    assert all(window.from_date == window.to_date for window in result.saturated_windows)
    assert all(window.returned_row_count == 2000 for window in result.saturated_windows)
    assert all(window.saturated is True for window in result.saturated_windows)


def test_priority_merge_keeps_every_corpus_candidate_and_preserves_foreign_event_country():
    def corpus_candidate(key: str, country: str):
        return ChessResultsCandidate(
            provider="chess-results",
            external_id=f"tnr{key}",
            source_url=f"https://chess-results.com/tnr{key}.aspx",
            title=f"Corpus {key}",
            time_control_hint="standard",
            evidence=("corpus:year:2026",),
            event_country=country,
            from_date="2026-03-01",
            to_date="2026-03-02",
            database_key=key,
            provider_game_count=20,
        )

    corpus = [
        corpus_candidate("1001", "VIE"),
        corpus_candidate("1002", "THA"),
        corpus_candidate("1003", "GER"),
        corpus_candidate("1004", "ARG"),
    ]
    player = ChessResultsCandidate(
        provider="chess-results",
        external_id="tnr1002",
        source_url="https://chess-results.com/tnr1002.aspx",
        title="5th Blue Chevaliers International Open 2026",
        time_control_hint="standard",
        evidence=("player_database:fide_id:12401064",),
        player_name="Nguyen, Van Huy",
        fide_id=12401064,
        source_fed="VIE",
        confirmed_vie=True,
        player_evidence=(
            PlayerEvidence(
                player_name="Nguyen, Van Huy",
                fide_id=12401064,
                source_fed="VIE",
                confirmed_vie=True,
                vietnamese_name_hint="strong",
            ),
        ),
    )
    unrelated_same_title = ChessResultsCandidate(
        provider="chess-results",
        external_id="tnr9999",
        source_url="https://chess-results.com/tnr9999.aspx",
        title="Corpus 1002",
        time_control_hint="standard",
        evidence=("player_database:fide_id:12401064",),
        source_fed="VIE",
        confirmed_vie=True,
    )

    merged = merge_corpus_priority(
        corpus,
        [DiscoveryResult((player, unrelated_same_title), ())],
    )

    assert [candidate.external_id for candidate in merged] == [
        "tnr1002",
        "tnr1001",
        "tnr1003",
        "tnr1004",
    ]
    assert len(merged) == 4
    foreign = next(candidate for candidate in merged if candidate.external_id == "tnr1002")
    assert foreign.event_country == "THA"
    assert foreign.source_fed == "VIE"
    assert foreign.priority_tier == "BOOK_HIGH"
    assert next(candidate for candidate in merged if candidate.external_id == "tnr1001").priority_tier == "BOOK_MEDIUM"
    assert {
        candidate.priority_tier
        for candidate in merged
        if candidate.external_id in {"tnr1003", "tnr1004"}
    } == {"GENERAL"}


def test_foreign_blue_chevaliers_keeps_tha_country_when_vie_player_matches_exact_tnr():
    corpus_candidate = ChessResultsCandidate(
        provider="chess-results",
        external_id="tnr1365480",
        source_url="https://chess-results.com/tnr1365480.aspx",
        title="5th Blue Chevaliers International Open 2026",
        time_control_hint="standard",
        evidence=("corpus:year:2026",),
        event_country="THA",
        from_date="2026-03-01",
        to_date="2026-03-08",
        database_key="1365480",
        provider_game_count=300,
    )
    player_match = ChessResultsCandidate(
        provider="chess-results",
        external_id="tnr1365480",
        source_url="https://chess-results.com/tnr1365480.aspx",
        title=corpus_candidate.title,
        time_control_hint="standard",
        evidence=("overseas_vie",),
        player_name="Nguyen, Van Huy",
        fide_id=12401064,
        source_fed="VIE",
        confirmed_vie=True,
        vietnamese_name_hint="strong",
        player_evidence=(
            PlayerEvidence(
                player_name="Nguyen, Van Huy",
                fide_id=12401064,
                source_fed="VIE",
                confirmed_vie=True,
                vietnamese_name_hint="strong",
            ),
        ),
    )

    merged = merge_corpus_priority(
        (corpus_candidate,),
        (DiscoveryResult((player_match,), ()),),
    )

    assert len(merged) == 1
    assert merged[0].external_id == "tnr1365480"
    assert merged[0].event_country == "THA"
    assert merged[0].priority_tier == "BOOK_HIGH"
    assert merged[0].player_name == "Nguyen, Van Huy"
    assert merged[0].fide_id == 12401064
