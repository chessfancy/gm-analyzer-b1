from __future__ import annotations

from urllib.parse import urlparse

import pytest

from chessgrandmaster.discovery.chess_results import (
    ChessResultsCandidate,
    ChessResultsDiscovery,
    _ordered_unique,
)


FEDERATION_HTML = """
<!doctype html>
<html><body>
  <h1>Federation: Vietnam (VIE)</h1>
  <table>
    <tr><td>St</td><td><a href="https://s3.chess-results.com/tnr1001.aspx?lan=1">Standard Open</a></td></tr>
    <tr><td>St</td><td><a href="https://s3.chess-results.com/tnr1001.aspx?lan=1">Standard Open duplicate</a></td></tr>
    <tr><td>Rp</td><td><a href="/tnr1002.aspx?lan=1">Rapid Cup</a></td></tr>
    <tr><td>Bz</td><td><a href="/tnr1003.aspx?lan=1">Blitz Cup</a></td></tr>
    <tr><td>?</td><td><a href="/tnr1004.aspx?lan=1">Mystery Cup</a></td></tr>
    <tr><td>bad</td><td><a href="/event.aspx?id=tnr9999">Malformed</a></td></tr>
    <tr><td>bad</td><td><a href="https://evil.example/tnr1005.aspx">Cross host</a></td></tr>
  </table>
</body></html>
"""


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


class _Opener:
    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[tuple[str, str, bytes | None]] = []

    def open(self, request, timeout: float):
        self.calls.append((request.method, request.full_url, request.data))
        return _Response(self.body, request.full_url)


PLAYER_FORM_HTML = """
<html><body>
  <form action="/custom-player-search" method="post">
    <input type="hidden" name="state-token" value="fixture-state">
    <label for="country-field">Country (FIDE abbreviation) *)</label>
    <input id="country-field" name="source-code" type="text">
    <label for="fide-field">Fide-ID *)</label>
    <input id="fide-field" name="player-reference" type="text">
    <label for="last-name-field">Last name</label>
    <input id="last-name-field" name="last-name" type="text">
    <label for="from-field">Tournament end between from</label>
    <input id="from-field" name="start-date" type="date">
    <label for="to-field">Tournament end between to</label>
    <input id="to-field" name="end-date" type="date">
    <label for="overseas-field">Only tournaments overseas</label>
    <input id="overseas-field" name="foreign-only" type="checkbox" value="yes">
    <button name="search-now" value="Find records">Find records</button>
    <button name="download-now" value="Download Excel">Download Excel</button>
  </form>
</body></html>
"""

PLAYER_RESULT_HTML = """
<html><body>
  <table>
    <tr><th>Name</th><th>FideID</th><th>FED</th><th>Tournament</th><th>End-Date</th></tr>
    <tr>
      <td><a href="/tnr2001.aspx?lan=1&art=9&snr=1">Le, Quang Liem</a></td>
      <td>12401137</td><td>VIE</td>
      <td><a href="/tnr2001.aspx?lan=1">Overseas Standard</a></td><td>2025/04/21</td>
    </tr>
    <tr>
      <td><a href="/tnr2001.aspx?lan=1&art=9&snr=2">Le, Quang Liem</a></td>
      <td>12401137</td><td>VIE</td>
      <td><a href="/tnr2001.aspx?lan=1">Overseas Standard</a></td><td>2025/04/21</td>
    </tr>
    <tr>
      <td><a href="/tnr2002.aspx?lan=1&art=9&snr=3">Le, Quang Liem</a></td>
      <td>12401137</td><td>VIE</td>
      <td><a href="/tnr2002.aspx?lan=1">Overseas Rapid</a></td><td>2026/08/11</td>
    </tr>
  </table>
</body></html>
"""


class _SequenceOpener:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str, bytes | None]] = []

    def open(self, request, timeout: float):
        self.calls.append((request.method, request.full_url, request.data))
        return _Response(self.pages[request.full_url], request.full_url)


def test_federation_feed_extracts_types_dedupes_and_rejects_unsafe_links():
    opener = _Opener(FEDERATION_HTML)
    service = ChessResultsDiscovery(opener=opener)

    result = service.discover_federation("VIE")

    assert [candidate.external_id for candidate in result.candidates] == [
        "tnr1001",
        "tnr1002",
        "tnr1003",
        "tnr1004",
    ]
    assert [candidate.time_control_hint for candidate in result.candidates] == [
        "standard",
        "rapid",
        "blitz",
        None,
    ]
    assert all(candidate.provider == "chess-results" for candidate in result.candidates)
    assert all(candidate.evidence == ("federation_feed:VIE",) for candidate in result.candidates)
    assert all(urlparse(candidate.source_url).hostname == "s3.chess-results.com" for candidate in result.candidates[:1])
    assert len(opener.calls) == 1
    assert opener.calls[0][0] == "GET"
    assert "fed=VIE" in opener.calls[0][1]


def test_candidate_ordering_prioritizes_confirmation_then_name_then_time_control():
    def candidate(external_id, *, hint="none", confirmed=False, time_control=None):
        return ChessResultsCandidate(
            provider="chess-results",
            external_id=external_id,
            source_url=f"https://chess-results.com/{external_id}.aspx",
            title=external_id,
            time_control_hint=time_control,
            evidence=(),
            vietnamese_name_hint=hint,
            confirmed_vie=confirmed,
        )

    ordered = _ordered_unique(
        [
            candidate("tnr7"),
            candidate("tnr6", time_control="blitz"),
            candidate("tnr5", time_control="rapid"),
            candidate("tnr4", time_control="standard"),
            candidate("tnr3", hint="strong", time_control="blitz"),
            candidate("tnr2", confirmed=True, time_control="rapid"),
            candidate("tnr1", confirmed=True, time_control="standard"),
        ]
    )

    assert [item.external_id for item in ordered] == [
        "tnr1",
        "tnr2",
        "tnr3",
        "tnr4",
        "tnr5",
        "tnr6",
        "tnr7",
    ]


def test_player_fide_search_discovers_rows_and_preserves_form_contract():
    entry_url = "https://chess-results.com/spielersuche.aspx?lan=1"
    search_url = "https://chess-results.com/custom-player-search"
    opener = _SequenceOpener(
        {entry_url: PLAYER_FORM_HTML, search_url: PLAYER_RESULT_HTML}
    )
    service = ChessResultsDiscovery(opener=opener)

    result = service.discover_player(
        "12401137",
        from_date="2025-01-01",
        to_date="2026-09-15",
        limit=10,
    )

    assert [candidate.external_id for candidate in result.candidates] == [
        "tnr2001",
        "tnr2002",
    ]
    assert result.candidates[0].confirmed_vie is True
    assert result.candidates[0].fide_id == 12401137
    assert result.candidates[0].source_fed == "VIE"
    assert result.candidates[0].evidence == (
        "player_database:fide_id:12401137",
    )
    assert len(opener.calls) == 2
    assert opener.calls[0][0] == "GET"
    assert opener.calls[1][0] == "POST"
    assert opener.calls[1][1] == search_url
    submitted = opener.calls[1][2].decode("utf-8")
    assert "state-token=fixture-state" in submitted
    assert "player-reference=12401137" in submitted
    assert "start-date=2025-01-01" in submitted
    assert "end-date=2026-09-15" in submitted
    assert "search-now=Find+records" in submitted
    assert "download-now" not in submitted


def test_player_form_rejects_cross_origin_action():
    entry_url = "https://chess-results.com/spielersuche.aspx?lan=1"
    evil_form = PLAYER_FORM_HTML.replace(
        'action="/custom-player-search"',
        'action="https://www.chess-results.com/custom-player-search"',
    )
    opener = _SequenceOpener({entry_url: evil_form})

    with pytest.raises(RuntimeError, match="cross-origin"):
        ChessResultsDiscovery(opener=opener).discover_player("12401137")
    assert len(opener.calls) == 1


def test_overseas_vie_search_sets_federation_and_overseas_semantics():
    entry_url = "https://chess-results.com/spielersuche.aspx?lan=1"
    search_url = "https://chess-results.com/custom-player-search"
    opener = _SequenceOpener(
        {entry_url: PLAYER_FORM_HTML, search_url: PLAYER_RESULT_HTML}
    )
    service = ChessResultsDiscovery(opener=opener)

    result = service.discover_overseas_vie(
        from_date="2025-01-01",
        to_date="2026-09-15",
        limit=1,
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].confirmed_vie is True
    submitted = opener.calls[1][2].decode("utf-8")
    assert "source-code=VIE" in submitted
    assert "foreign-only=yes" in submitted
    assert "start-date=2025-01-01" in submitted
    assert "end-date=2026-09-15" in submitted


DIASPORA_RESULT_HTML = """
<html><body>
  <table>
    <tr><th>Name</th><th>FideID</th><th>FED</th><th>Tournament</th><th>End-Date</th></tr>
    <tr>
      <td><a href="/tnr3001.aspx?lan=1&art=9&snr=1">Nguyen, Thai Dai Van</a></td>
      <td>358878</td><td>CZE</td>
      <td><a href="/tnr3001.aspx?lan=1">Diaspora Standard</a></td><td>2025/04/21</td>
    </tr>
    <tr>
      <td><a href="/tnr3001.aspx?lan=1&art=9&snr=2">Nguyen, Minh Anh</a></td>
      <td>900000000</td><td>CZE</td>
      <td><a href="/tnr3001.aspx?lan=1">Diaspora Standard</a></td><td>2025/04/21</td>
    </tr>
    <tr>
      <td><a href="/tnr3002.aspx?lan=1&art=9&snr=3">Hong, Lee</a></td>
      <td>123456</td><td>CZE</td>
      <td><a href="/tnr3002.aspx?lan=1">Weak Name Cup</a></td><td>2025/04/22</td>
    </tr>
  </table>
</body></html>
"""


def test_diaspora_lane_keeps_only_strong_name_hints_and_preserves_source_fed():
    entry_url = "https://chess-results.com/spielersuche.aspx?lan=1"
    search_url = "https://chess-results.com/custom-player-search"
    opener = _SequenceOpener(
        {entry_url: PLAYER_FORM_HTML, search_url: DIASPORA_RESULT_HTML}
    )
    service = ChessResultsDiscovery(opener=opener)

    result = service.discover_diaspora(
        from_date="2025-01-01",
        to_date="2026-09-15",
        limit=10,
        surname_seeds=("nguyen",),
    )

    assert [candidate.external_id for candidate in result.candidates] == ["tnr3001"]
    candidate = result.candidates[0]
    assert candidate.player_name == "Nguyen, Thai Dai Van"
    assert candidate.fide_id == 358878
    assert candidate.source_fed == "CZE"
    assert candidate.confirmed_vie is False
    assert candidate.vietnamese_name_hint == "strong"
    assert {
        "name_hint:strong",
        "surname_seed:nguyen",
        "source_fed:CZE",
    } <= set(candidate.evidence)
    assert len(candidate.player_evidence) == 2
    submitted = opener.calls[1][2].decode("utf-8")
    assert "last-name=nguyen" in submitted
    assert "foreign-only" not in submitted


FAMILY_HTML = """
<html><body>
  <h1>National Youth Championship U07</h1>
  <nav id="tournament-family" aria-label="Tournament sections">
    <a href="/tnr1450898.aspx?lan=1">U09</a>
    <a href="/tnr1450911.aspx?lan=1">U11</a>
  </nav>
  <p><a href="/tnr1450910.aspx?lan=1">Ordinary related-looking link</a></p>
  <form id="form1">
    <select name="section-selector" aria-label="Tournament divisions">
      <option value="tnr1450912">Girls U09</option>
    </select>
  </form>
</body></html>
"""


def test_family_expansion_follows_only_explicit_provider_relations():
    seed_url = "https://s3.chess-results.com/tnr1450909.aspx?lan=1"
    opener = _SequenceOpener({seed_url: FAMILY_HTML})
    service = ChessResultsDiscovery(opener=opener)

    result = service.expand_family(seed_url)

    assert [candidate.external_id for candidate in result.candidates] == [
        "tnr1450898",
        "tnr1450911",
        "tnr1450912",
    ]
    assert "tnr1450910" not in {candidate.external_id for candidate in result.candidates}
    assert all("family_relation:" in candidate.evidence[0] for candidate in result.candidates)
    assert len(opener.calls) == 1


def test_family_expansion_reports_unsupported_without_explicit_relation():
    seed_url = "https://s3.chess-results.com/tnr1450909.aspx?lan=1"
    no_family_html = """
    <html><body>
      <h1>Single section</h1>
      <p><a href="/tnr1450910.aspx?lan=1">Ordinary link</a></p>
    </body></html>
    """
    result = ChessResultsDiscovery(
        opener=_SequenceOpener({seed_url: no_family_html})
    ).expand_family(seed_url)

    assert result.candidates == ()
    assert result.errors == ("family:no explicit provider relation",)


def test_cli_emits_json_for_validation_error(capsys):
    from chessgrandmaster.discovery.cli import main

    assert main(["chess-results", "federation", "bad!"]) == 2
    captured = capsys.readouterr()
    payload = __import__("json").loads(captured.out)
    assert payload["ok"] is False
    assert payload["provider"] == "chess-results"
    assert payload["mode"] == "federation"
    assert captured.err == ""


def test_cli_success_preserves_candidate_count_and_partial_errors(monkeypatch, capsys):
    from chessgrandmaster.discovery import chess_results
    from chessgrandmaster.discovery.cli import main

    candidate = chess_results.ChessResultsCandidate(
        provider="chess-results",
        external_id="tnr5001",
        source_url="https://chess-results.com/tnr5001.aspx?lan=1",
        title="Fixture tournament",
        time_control_hint="standard",
        evidence=("family_relation:explicit_link",),
    )

    class _FakeService:
        def __init__(self, **_kwargs):
            pass

        def expand_family(self, seed, *, limit=None):
            assert seed == "tnr5000"
            assert limit == 7
            return chess_results.DiscoveryResult((candidate,), ("family:partial",))

    monkeypatch.setattr("chessgrandmaster.discovery.cli.ChessResultsDiscovery", _FakeService)
    assert main(["chess-results", "family", "tnr5000", "--limit", "7"]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["candidate_count"] == 1
    assert len(payload["candidates"]) == 1
    assert payload["errors"] == ["family:partial"]
