# Live Source Finalization: TWIC Direct ZIP, Chess.com Deferred

Date: 2026-09-21
Branch: `chatgpt-work`
Investigated against live production sites by ChatGPT.

## Decision

- **TWIC stays active** and should be finalized using its deterministic ZIP URL.
- **Chess.com/Chess24 is deferred** from the production ingestion path for now.
- Lichess Broadcast remains the live Olympiad source and is already accepted.

Do not spend more implementation time on Chess.com during the current
B2a/B2b/Oracle cutover milestone.

## Chess.com investigation

Live page inspected:

```text
https://www.chess.com/events/2026-fide-chess-olympiad-open/dashboard/05/Svane_Frederik-Niemann_Hans_Moke
```

The page is a genuine OTB Olympiad event page.

The HTML exposes:

```text
window.chesscom.events.chessbombApiUrl =
https://www.chess.com/events/v1/api/
```

The public structured room call works:

```text
POST https://www.chess.com/events/v1/api/room/2026-fide-chess-olympiad-open
```

Observed response includes:

- room id: `24873`
- round 05 id: `219732`
- requested game id: `5604392`
- slug: `Svane_Frederik-Niemann_Hans_Moke`
- result: `1-0`
- player/FIDE/rating metadata
- current FEN
- `metadata.isChesscomOnlineGame = false`

However the room response does **not** contain the move list for the game.

The current frontend bundle shows that full game moves are requested
through the Events PubSub path using message type `GET_GAME` with:

```text
roomSlug
roundSlug
gameSlug
markerMoves
markerAnalysis
fullState
```

That is not the same as a simple public HTTP
`/events/v1/api/game/<slug>` endpoint.

The frontend also exposes an official PGN download route named
`web_event_download_pgn`, whose effective path is:

```text
/events/pgn/{eventId}/{roundId}
```

The UI requests all rounds with `roundId = 0`.

Live unauthenticated probes:

```text
https://www.chess.com/events/pgn/24873/0
https://www.chess.com/events/pgn/24873/219732
```

both redirect to `login_and_go`, so the official PGN download is
authenticated.

Therefore the current production decision is:

- do not scrape rendered boards;
- do not reverse-engineer the PubSub websocket now;
- do not store Chess.com account cookies/credentials on Oracle merely to
  gain a duplicate PGN source;
- keep the adapter code dormant/experimental if useful, but do not
  schedule it in the Oracle production ingestion loop.

Revisit Chess.com only if a stable unauthenticated structured move/PGN
endpoint becomes available or there is a separate high-value reason to
support authenticated ingestion.

## TWIC investigation

TWIC issue downloads are deterministic.

For issue `N`:

```text
archive page:
https://theweekinchess.com/html/twicN.html

PGN ZIP:
https://theweekinchess.com/zips/twicNg.zip
```

Examples:

```text
https://theweekinchess.com/zips/twic1660g.zip
https://theweekinchess.com/zips/twic1661g.zip
```

Do not depend on parsing an HTML anchor to discover the PGN URL.
Construct it directly from the validated issue number.

### Live Oracle acceptance evidence

Using the current TWIC request headers on `Oracle-Chess`:

```text
User-Agent: ChessGrandmaster acquisition/1.0
Accept: application/zip,application/octet-stream,*/*
Accept-Encoding: identity
Referer: https://theweekinchess.com/
```

direct ZIP requests returned HTTP 200.

Observed issue 1660:

```text
ZIP URL:      https://theweekinchess.com/zips/twic1660g.zip
ZIP bytes:    2,600,817
ZIP member:   twic1660.pgn
PGN bytes:    9,124,872
Event tags:   9,139
```

Observed issue 1661:

```text
ZIP URL:      https://theweekinchess.com/zips/twic1661g.zip
ZIP bytes:    2,220,223
ZIP member:   twic1661.pgn
PGN bytes:    7,758,714
Event tags:   7,670
```

Those counts match the live TWIC archive listings.

The current `TwicAdapter` at commit
`6c7ab2d43824c4be99ed28dfc040a769cf0e782d` was also live-tested on
Oracle:

```text
twic:1660
discover -> PASS
describe -> https://theweekinchess.com/zips/twic1660g.zip
download_pgn -> PASS
extracted PGN -> 9,124,872 bytes / 9,139 Event tags

twic:1661
discover -> PASS
describe -> https://theweekinchess.com/zips/twic1661g.zip
download_pgn -> PASS
extracted PGN -> 7,758,714 bytes / 7,670 Event tags
```

So TWIC is not currently blocked on Oracle.

## Luna implementation instruction

This is a small robustness cleanup, not a redesign.

1. Add/retain one deterministic helper equivalent to:

```python
def _pgn_zip_url(self, issue: int) -> str:
    return f"{self.base_url}/zips/twic{issue}g.zip"
```

2. For a validated issue number, the deterministic ZIP URL is the
   authoritative PGN download URL. HTML link discovery must not be
   required to obtain it.

3. `describe()` may fetch the issue HTML for optional human-readable
   metadata, but failure to find a PGN anchor must not make the source
   unavailable. It must still return the deterministic ZIP URL.

4. `download_pgn()` must use the same cookie-aware opener/session and
   the TWIC-specific request headers already used by the shared HTTP
   helper.

5. If a provider-side HTTP 406 is observed, allow at most one bounded
   warm-up GET to the same-origin TWIC archive/index page, then retry the
   deterministic ZIP once. Do not add a large retry framework.

6. Validate the response as a ZIP:
   - non-empty;
   - within existing archive size limit;
   - ZIP magic/zipfile validation;
   - at least one `.pgn` member;
   - prefer the member whose stem contains the requested issue number;
   - validate extracted PGN signature;
   - write atomically.

7. Do not hardcode the observed byte sizes or game counts into production
   logic. They are acceptance evidence only.

8. Do not crawl multiple issues during tests. Bounded live acceptance:
   issue 1660 and optionally one adjacent issue 1661.

9. Keep source provenance as `twic` / `twicNNNN`. Do not change
   canonical fingerprint semantics.

10. Do not claim the entire TWIC issue is classical based only on issue
    page text. TWIC issues can contain mixed time controls. Standard/
    classical filtering belongs at game/event classification, not by
    assuming the whole weekly bundle is one time class.

## Acceptance for this task

On `Oracle-Chess`:

```text
twic:1660
-> discover
-> describe
-> pgn_url exactly https://theweekinchess.com/zips/twic1660g.zip
-> download_pgn
-> valid non-empty extracted PGN
-> approximately 9,139 games/event headers

twic:1661
-> same path with twic1661g.zip
-> valid non-empty extracted PGN
-> approximately 7,670 games/event headers
```

Then run normal focused/full regression, compileall and
`git diff --check`.

No production corpus writes during implementation.

## After TWIC finalization

Proceed to the Windows -> Oracle authoritative corpus cutover.

Production scheduled ingestion on Oracle should initially enable:

1. Chess-Results
2. Lichess Broadcast (live Olympiad)
3. TWIC

Chess.com remains disabled/deferred.
