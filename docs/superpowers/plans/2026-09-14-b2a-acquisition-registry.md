# B2a Acquisition and Tournament Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic B2a pipeline that accepts a Chess-Results tournament reference, preserves immutable raw PGN, registers provenance in `registry.sqlite`, deduplicates exact chess content globally, and emits a canonical tournament revision ready for B2b packaging.

**Architecture:** Add a new `chessgrandmaster.b2` package beside the frozen B1 analyzer. B2a is split into source adapters, immutable object storage, registry persistence, chess-content identity/canonicalization, and explainable tournament priority; B1 production modules are read-only dependencies. The first source path is direct Chess-Results ingestion, using `tnr1450909` as the live acceptance seed while normal tests remain fixture-only.

**Tech Stack:** Python 3.11+, `python-chess`, stdlib `sqlite3`, `urllib.request`, `html.parser`, `hashlib`, `json`, `pathlib`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-14-b2-acquisition-workload-design.md`

## Global Constraints

- Work only on `chatgpt-work`; do not merge to `main`, create a PR, force-push, or change the canonical upstream without explicit approval.
- B1 is frozen: do not change Lucas classification, Stockfish search policy, worker scheduling, platform Hash policy, PGN export semantics, or `PIPELINE_VERSION = 3`.
- Raw source files are immutable and addressable by content SHA256.
- Automatic dedupe is exact chess-content only: `game_fingerprint_v1 = SHA256(variant + normalized_initial_fen + ordered_mainline_uci_moves)`.
- Metadata similarity, player-name similarity, prefix/truncated games, and sibling Chess-Results tournament numbers must not auto-merge in v1.
- One global canonical game may have many source occurrences and may belong to many tournament revisions.
- A canonical game may appear at most once inside one tournament revision.
- Normal tests must not depend on live Chess-Results or network credentials.
- The supplied live seed `https://s3.chess-results.com/tnr1450909.aspx?lan=1&art=0&turdet=YES&SNode=S0` is treated as one source section (`tnr1450909`), not automatically grouped with neighboring age/gender section IDs.
- Do not hard-code Vietnamese provincial Chess-Results federation abbreviations as FIDE federation truth. V1 priority may use explicit `VIE` player/event hints; broader domestic identity enrichment is deferred.

---

## File Structure

Create a focused B2 package; do not add B2 responsibilities to `production_pipeline.py`.

```text
src/chessgrandmaster/b2/
    __init__.py
    json_codec.py          # canonical JSON + SHA helpers
    identity.py            # game_fingerprint_v1 and normalized initial state
    storage.py             # ObjectStore protocol + LocalObjectStore
    registry.py            # registry.sqlite schema/migrations/repository
    priority.py            # Vietnamese/OTB/classical explainable scoring
    canonicalize.py        # PGN parse, occurrences, exact dedupe, revision output
    acquisition.py         # orchestration from SourceRef to canonical revision
    cli_acquire.py         # cgm-acquire
    cli_registry.py        # cgm-registry
    sources/
        __init__.py
        base.py            # SourceRef/SourceDescriptor/SourceAdapter contracts
        chess_results.py   # Chess-Results URL/page/PGN adapter

tests/b2/
    fixtures/
        chess_results_tournament.html
        chess_results_games.pgn
        duplicate_conflict_a.pgn
        duplicate_conflict_b.pgn
        truncated_games.pgn
    test_json_codec.py
    test_identity.py
    test_storage.py
    test_registry.py
    test_priority.py
    test_chess_results_source.py
    test_canonicalize.py
    test_acquisition.py
    test_b2a_cli.py
```

Modify:

```text
pyproject.toml             # add cgm-acquire and cgm-registry entry points
```

---

### Task 1: Canonical JSON and game identity primitives

**Files:**
- Create: `src/chessgrandmaster/b2/__init__.py`
- Create: `src/chessgrandmaster/b2/json_codec.py`
- Create: `src/chessgrandmaster/b2/identity.py`
- Test: `tests/b2/test_json_codec.py`
- Test: `tests/b2/test_identity.py`

**Interfaces:**
- Produces: `canonical_json_bytes(value: object) -> bytes`
- Produces: `canonical_json_sha256(value: object) -> str`
- Produces: `GameIdentity`
- Produces: `normalize_initial_fen(board: chess.Board) -> str`
- Produces: `identify_game(game: chess.pgn.Game) -> GameIdentity`

- [ ] **Step 1: Write failing canonical JSON tests**

```python
from chessgrandmaster.b2.json_codec import canonical_json_bytes, canonical_json_sha256


def test_canonical_json_is_order_independent():
    left = {"b": 2, "a": {"y": 2, "x": 1}}
    right = {"a": {"x": 1, "y": 2}, "b": 2}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_sha256(left) == canonical_json_sha256(right)


def test_canonical_json_is_utf8_and_compact():
    payload = {"name": "Nguyễn", "value": 1}
    assert canonical_json_bytes(payload) == (
        '{"name":"Nguyễn","value":1}'.encode("utf-8")
    )
```

- [ ] **Step 2: Run the JSON tests and verify they fail**

Run: `pytest tests/b2/test_json_codec.py -v`

Expected: FAIL because `chessgrandmaster.b2.json_codec` does not exist.

- [ ] **Step 3: Implement canonical JSON helpers**

```python
# src/chessgrandmaster/b2/json_codec.py
import hashlib
import json


def canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
```

- [ ] **Step 4: Write failing game-identity tests**

```python
import io
import chess.pgn

from chessgrandmaster.b2.identity import identify_game


def read_one(text):
    game = chess.pgn.read_game(io.StringIO(text))
    assert game is not None
    assert not game.errors
    return game


def test_fingerprint_ignores_headers_comments_nags_and_variations():
    a = read_one('''
[Event "Source A"]
[White "Nguyen A"]
[Black "Player B"]
[Result "1-0"]

1. e4 $1 {comment} e5 (1... c5) 2. Nf3 Nc6 1-0
''')
    b = read_one('''
[Event "Different spelling"]
[White "NGUYEN, A"]
[Black "Player B"]
[Result "*"]

1.e4 e5 2.Nf3 Nc6 *
''')
    assert identify_game(a).fingerprint == identify_game(b).fingerprint


def test_fingerprint_changes_when_mainline_changes():
    a = read_one('[Result "*"]\n\n1. e4 e5 2. Nf3 Nc6 *')
    b = read_one('[Result "*"]\n\n1. e4 c5 2. Nf3 d6 *')
    assert identify_game(a).fingerprint != identify_game(b).fingerprint


def test_normalized_default_start_does_not_depend_on_fen_header_presence():
    a = read_one('[Result "*"]\n\n1. e4 *')
    b = read_one('''
[SetUp "1"]
[FEN "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"]
[Result "*"]

1. e4 *
''')
    assert identify_game(a).fingerprint == identify_game(b).fingerprint
```

- [ ] **Step 5: Run identity tests and verify failure**

Run: `pytest tests/b2/test_identity.py -v`

Expected: FAIL because identity functions are missing.

- [ ] **Step 6: Implement exact chess-content identity**

```python
# src/chessgrandmaster/b2/identity.py
from dataclasses import dataclass
import hashlib

import chess
import chess.pgn


FINGERPRINT_VERSION = "game_fingerprint_v1"


@dataclass(frozen=True)
class GameIdentity:
    fingerprint_version: str
    fingerprint: str
    variant: str
    initial_fen: str
    mainline_uci: tuple[str, ...]
    ply_count: int


def normalize_initial_fen(board):
    # Piece placement, turn, castling and en-passant affect chess state.
    # Halfmove/fullmove counters are formatting/history details for identity.
    return " ".join(board.fen(en_passant="fen").split()[:4])


def _variant_name(game):
    value = game.headers.get("Variant", "Standard").strip().casefold()
    return value or "standard"


def identify_game(game):
    board = game.board()
    initial_fen = normalize_initial_fen(board)
    moves = tuple(move.uci() for move in game.mainline_moves())
    variant = _variant_name(game)
    payload = "\n".join((variant, initial_fen, *moves)).encode("utf-8")
    fingerprint = hashlib.sha256(payload).hexdigest()
    return GameIdentity(
        fingerprint_version=FINGERPRINT_VERSION,
        fingerprint=fingerprint,
        variant=variant,
        initial_fen=initial_fen,
        mainline_uci=moves,
        ply_count=len(moves),
    )
```

- [ ] **Step 7: Run focused tests**

Run: `pytest tests/b2/test_json_codec.py tests/b2/test_identity.py -v`

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```bash
git add src/chessgrandmaster/b2 tests/b2/test_json_codec.py tests/b2/test_identity.py
git commit -m "feat: add B2 canonical game identity"
```

---

### Task 2: Local immutable object storage

**Files:**
- Create: `src/chessgrandmaster/b2/storage.py`
- Test: `tests/b2/test_storage.py`

**Interfaces:**
- Produces: `ObjectStat(key: str, size: int, sha256: str)`
- Produces: `ObjectStore` protocol with `put_file`, `get_file`, `exists`, `stat`, `list`
- Produces: `LocalObjectStore(root: Path)`

- [ ] **Step 1: Write failing LocalObjectStore tests**

```python
from pathlib import Path
import hashlib

from chessgrandmaster.b2.storage import LocalObjectStore


def test_local_store_round_trip_and_checksum(tmp_path):
    source = tmp_path / "source.pgn"
    source.write_bytes(b"[Result \"*\"]\n\n*\n")
    store = LocalObjectStore(tmp_path / "objects")

    stat = store.put_file("raw/abc/original.pgn", source)
    assert stat.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert store.exists("raw/abc/original.pgn")

    restored = tmp_path / "restored.pgn"
    store.get_file("raw/abc/original.pgn", restored)
    assert restored.read_bytes() == source.read_bytes()


def test_local_store_refuses_overwrite_with_different_bytes(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    a = tmp_path / "a"; a.write_bytes(b"a")
    b = tmp_path / "b"; b.write_bytes(b"b")
    store.put_file("immutable/key", a)
    try:
        store.put_file("immutable/key", b)
    except ValueError as exc:
        assert "immutable" in str(exc).lower()
    else:
        raise AssertionError("different bytes must not overwrite immutable key")
```

- [ ] **Step 2: Run test and verify failure**

Run: `pytest tests/b2/test_storage.py -v`

Expected: FAIL because `storage.py` does not exist.

- [ ] **Step 3: Implement the storage protocol and LocalObjectStore**

Implementation requirements:

```python
@dataclass(frozen=True)
class ObjectStat:
    key: str
    size: int
    sha256: str

class ObjectStore(Protocol):
    def put_file(self, key: str, path: Path) -> ObjectStat: ...
    def get_file(self, key: str, destination: Path) -> ObjectStat: ...
    def exists(self, key: str) -> bool: ...
    def stat(self, key: str) -> ObjectStat: ...
    def list(self, prefix: str) -> list[str]: ...
```

`LocalObjectStore.put_file()` must copy through a temporary sibling file, `fsync`, verify SHA256, and use `os.replace()` only when the destination is absent. If the destination exists with the same SHA, return its stat; if bytes differ, raise `ValueError`.

- [ ] **Step 4: Run storage tests**

Run: `pytest tests/b2/test_storage.py -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/chessgrandmaster/b2/storage.py tests/b2/test_storage.py
git commit -m "feat: add immutable local object store"
```

---

### Task 3: Registry schema and idempotent migrations

**Files:**
- Create: `src/chessgrandmaster/b2/registry.py`
- Test: `tests/b2/test_registry.py`

**Interfaces:**
- Produces: `REGISTRY_SCHEMA_VERSION = 1`
- Produces: `Registry(path: Path)`
- Produces methods used later: `ensure_schema`, `upsert_source`, `upsert_tournament`, `upsert_source_tournament`, `record_source_file`, `record_download_attempt`, `upsert_canonical_game`, `record_occurrence`, `replace_metadata_conflicts`, `create_or_get_revision`, `set_revision_games`, `get_tournament_status`

- [ ] **Step 1: Write failing schema/idempotency tests**

Tests must create a fresh SQLite file, call `Registry.ensure_schema()` twice, and assert these tables exist:

```python
EXPECTED_TABLES = {
    "registry_meta",
    "sources",
    "tournaments",
    "source_tournaments",
    "source_files",
    "download_attempts",
    "canonical_games",
    "game_occurrences",
    "game_metadata_conflicts",
    "tournament_revisions",
    "tournament_games",
}
```

Also assert `PRAGMA foreign_keys=1` inside repository connections and uniqueness constraints by attempting duplicate `source_tournaments(source_id, external_id)` and duplicate `canonical_games(fingerprint_version, fingerprint)`.

- [ ] **Step 2: Run registry tests and verify failure**

Run: `pytest tests/b2/test_registry.py -v`

Expected: FAIL because `Registry` is missing.

- [ ] **Step 3: Implement schema version 1**

Use SQLite DDL with these required uniqueness rules:

```sql
UNIQUE(sources.name)
UNIQUE(source_tournaments.source_id, source_tournaments.external_id)
UNIQUE(source_files.source_tournament_id, source_files.sha256)
UNIQUE(canonical_games.fingerprint_version, canonical_games.fingerprint)
UNIQUE(game_occurrences.source_file_id, game_occurrences.source_game_index)
UNIQUE(tournament_revisions.tournament_id, tournament_revisions.revision_number)
UNIQUE(tournament_revisions.tournament_id, tournament_revisions.canonical_sha256, tournament_revisions.canonicalization_policy)
UNIQUE(tournament_games.revision_id, tournament_games.ordinal)
UNIQUE(tournament_games.revision_id, tournament_games.canonical_game_id)
UNIQUE(game_metadata_conflicts.canonical_game_id, game_metadata_conflicts.field)
```

Required state values are stored as text and validated in repository methods:

```text
DISCOVERED -> DOWNLOADED -> VALIDATED -> CANONICALIZED -> SHARDED -> READY
```

B2a will stop at `CANONICALIZED`; B2b owns `SHARDED` and `READY` transitions.

- [ ] **Step 4: Add repository behavior tests**

Test that:

1. two occurrences with the same fingerprint return the same `canonical_game_id`;
2. both occurrence rows remain present;
3. one canonical game can be inserted into revision A and revision B;
4. inserting the same canonical game twice into one revision does not create duplicate membership;
5. metadata conflict JSON is canonical and stable.

- [ ] **Step 5: Implement minimal repository methods to pass behavior tests**

Use explicit transactions (`with con:`), return integer IDs, and never expose callers to `sqlite3.Row` outside `registry.py`; return dictionaries/dataclasses instead.

- [ ] **Step 6: Run registry tests**

Run: `pytest tests/b2/test_registry.py -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/chessgrandmaster/b2/registry.py tests/b2/test_registry.py
git commit -m "feat: add B2 tournament registry"
```

---

### Task 4: Explainable Vietnamese/OTB/classical priority policy

**Files:**
- Create: `src/chessgrandmaster/b2/priority.py`
- Test: `tests/b2/test_priority.py`

**Interfaces:**
- Produces: `TournamentSignals`
- Produces: `PriorityResult(score: int, has_vietnamese_player: bool, reasons: tuple[str, ...])`
- Produces: `score_tournament(signals: TournamentSignals) -> PriorityResult`

- [ ] **Step 1: Write failing priority tests**

```python
from chessgrandmaster.b2.priority import TournamentSignals, score_tournament


def test_vie_player_is_highest_initial_signal():
    result = score_tournament(TournamentSignals(
        player_federations=("VIE", "SGP"),
        event_country=None,
        is_otb=True,
        time_control_class="classical",
        game_count=120,
        source_priority=50,
    ))
    assert result.has_vietnamese_player is True
    assert "player_federation:VIE" in result.reasons


def test_event_country_vie_is_recorded_without_inventing_player_identity():
    result = score_tournament(TournamentSignals(
        player_federations=(),
        event_country="VIE",
        is_otb=True,
        time_control_class="classical",
        game_count=200,
        source_priority=50,
    ))
    assert result.has_vietnamese_player is False
    assert "event_country:VIE" in result.reasons
```

This distinction is intentional: a Vietnamese-hosted event is valuable, but `has_vietnamese_player` only becomes true from player-level evidence.

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/b2/test_priority.py -v`

Expected: FAIL because priority module is missing.

- [ ] **Step 3: Implement deterministic initial scoring**

Use fixed weights and reason strings:

```python
PLAYER_VIE = 100
EVENT_VIE = 60
CLASSICAL = 30
OTB = 20
GAME_VOLUME_100 = 10
GAME_VOLUME_500 = 10
SOURCE_PRIORITY_SCALE = 10  # add source_priority // 10, capped at 10
```

Normalize federation/country values with `strip().upper()`. Do not infer `VIE` from Chess-Results domestic codes such as `HCM`, `HNO`, `DAN`, or club names.

- [ ] **Step 4: Run priority tests**

Run: `pytest tests/b2/test_priority.py -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/chessgrandmaster/b2/priority.py tests/b2/test_priority.py
git commit -m "feat: add explainable tournament priority"
```

---

### Task 5: Source adapter contract and Chess-Results direct-URL adapter

**Files:**
- Create: `src/chessgrandmaster/b2/sources/__init__.py`
- Create: `src/chessgrandmaster/b2/sources/base.py`
- Create: `src/chessgrandmaster/b2/sources/chess_results.py`
- Create: `tests/b2/fixtures/chess_results_tournament.html`
- Create: `tests/b2/fixtures/chess_results_games.pgn`
- Test: `tests/b2/test_chess_results_source.py`

**Interfaces:**
- Produces: `SourceRef(provider: str, external_id: str, source_url: str)`
- Produces: `SourceDescriptor(ref, title, pgn_url, event_country_hint, time_control_hint, is_otb_hint)`
- Produces: `SourceAdapter` protocol
- Produces: `ChessResultsAdapter(timeout_sec: float = 30.0, opener=None)`
- Produces: `ChessResultsAdapter.discover(query: str) -> list[SourceRef]`
- Produces: `describe(ref: SourceRef) -> SourceDescriptor`
- Produces: `download_pgn(ref: SourceRef, destination: Path) -> Path`

- [ ] **Step 1: Create fixture HTML representative of a Chess-Results tournament page**

Fixture must contain:

```html
<html><body>
<h2>Giải Vô địch Cờ vua trẻ quốc gia năm 2026</h2>
<a href="/tnr1450909.aspx?lan=1&art=0">Tournament</a>
<a href="/DownloadFile.aspx?file=Games.pgn">Games.pgn</a>
</body></html>
```

The fixture is contract data; live HTML may have additional attributes/layout.

- [ ] **Step 2: Write failing adapter tests**

Required assertions:

```python
adapter.discover("tnr1450909")[0].external_id == "tnr1450909"
adapter.discover("https://s3.chess-results.com/tnr1450909.aspx?lan=1")[0].external_id == "tnr1450909"
```

Also verify `s1`, `s2`, `s3`, `chess-results.com`, and `/test/tnrNNNN.aspx` forms all normalize to provider `chess-results` plus `tnrNNNN` external identity while preserving the originally supplied URL as provenance.

Use an injected fake opener to return fixture HTML/PGN bytes; no network calls in pytest.

- [ ] **Step 3: Run tests and verify failure**

Run: `pytest tests/b2/test_chess_results_source.py -v`

Expected: FAIL because adapter code is missing.

- [ ] **Step 4: Implement URL/ref parsing**

Use `urllib.parse.urlparse`, regex `r"tnr(\d+)\.aspx$"` against the final path component, and `urllib.parse.urljoin` for PGN links. `discover()` accepts only a `tnrNNNN` reference or URL containing one; other free-text queries raise a clear `ValueError` in v1 rather than pretending to implement broad web discovery.

- [ ] **Step 5: Implement page parsing and PGN-link selection**

Use `html.parser.HTMLParser`; collect `<h1>/<h2>` text and anchor `href` + text. Choose a PGN candidate when either normalized anchor text ends in `.pgn` or the URL path ends in `.pgn`. If no direct candidate exists, raise `RuntimeError("Chess-Results page exposes no downloadable PGN link")` and record the failed attempt at the acquisition layer.

Do not fabricate undocumented download URLs.

- [ ] **Step 6: Implement streaming PGN download**

Download to `destination.with_suffix(destination.suffix + ".part")`, write in 1 MiB chunks, `fsync`, then `os.replace`. Never return partially downloaded PGN under the final filename.

- [ ] **Step 7: Run adapter tests**

Run: `pytest tests/b2/test_chess_results_source.py -v`

Expected: PASS.

- [ ] **Step 8: Commit Task 5**

```bash
git add src/chessgrandmaster/b2/sources tests/b2/fixtures tests/b2/test_chess_results_source.py
git commit -m "feat: add Chess-Results acquisition adapter"
```

---

### Task 6: Exact dedupe, metadata conflicts, and canonical tournament revisions

**Files:**
- Create: `src/chessgrandmaster/b2/canonicalize.py`
- Create: `tests/b2/fixtures/duplicate_conflict_a.pgn`
- Create: `tests/b2/fixtures/duplicate_conflict_b.pgn`
- Create: `tests/b2/fixtures/truncated_games.pgn`
- Test: `tests/b2/test_canonicalize.py`

**Interfaces:**
- Produces: `CanonicalizationResult(tournament_id, revision_id, revision_number, canonical_sha256, game_count, ply_count, canonical_path)`
- Produces: `canonicalize_source_file(registry, tournament_id, source_file_id, raw_path, output_path, canonicalization_policy="canonical_pgn_v1") -> CanonicalizationResult`

- [ ] **Step 1: Write duplicate/conflict fixtures and failing tests**

Create two PGNs with the same legal mainline but conflicting headers (`Result`, spelling, comments), plus one prefix-truncated PGN. Tests must prove:

```text
same full mainline + different metadata -> one canonical_games row, two occurrences
conflicting Result -> one game_metadata_conflicts row with both values
prefix-only shorter game -> different fingerprint and separate canonical_games row
same fingerprint repeated inside one tournament revision -> one tournament_games membership
```

- [ ] **Step 2: Run canonicalizer tests and verify failure**

Run: `pytest tests/b2/test_canonicalize.py -v`

Expected: FAIL because canonicalizer is missing.

- [ ] **Step 3: Implement parse loop with per-game validation**

For every `chess.pgn.read_game()` result:

1. capture `source_game_index` starting at 1;
2. if `game.errors` is non-empty, register the raw occurrence as invalid evidence and exclude it from revision membership;
3. otherwise compute `GameIdentity`;
4. `upsert_canonical_game()` by exact fingerprint;
5. `record_occurrence()` with raw headers and source index;
6. recompute metadata conflicts for `Event`, `Site`, `Date`, `Round`, `White`, `Black`, `Result`;
7. append the canonical game only on first fingerprint occurrence in this revision.

- [ ] **Step 4: Implement deterministic canonical occurrence selection**

When a global canonical game has multiple valid occurrences, select display headers by this stable ordering:

```text
source.priority DESC
source.name ASC
source_file.sha256 ASC
source_game_index ASC
```

Store the selected occurrence ID on `canonical_games.canonical_occurrence_id`. This prevents canonical headers from depending on ingestion order.

- [ ] **Step 5: Implement deterministic canonical PGN projection**

For each revision member in original tournament order:

1. reconstruct a `chess.pgn.Game` from the selected occurrence's initial board and mainline moves;
2. copy deterministic display headers;
3. export with `chess.pgn.StringExporter(headers=True, variations=False, comments=False, columns=None)`;
4. separate games by exactly two newlines;
5. encode UTF-8 with `\n` line endings.

Raw comments/variations remain untouched in immutable source storage. Canonical PGN is deliberately an analysis projection.

- [ ] **Step 6: Make revision creation content-addressed and idempotent**

Hash canonical PGN bytes. If `(tournament_id, canonical_sha256, canonicalization_policy)` already exists, reuse its revision number and membership; otherwise allocate `MAX(revision_number)+1` inside the same transaction.

- [ ] **Step 7: Run canonicalizer tests twice**

Run:

```bash
pytest tests/b2/test_canonicalize.py -v
pytest tests/b2/test_canonicalize.py -v
```

Expected: both PASS with identical canonical SHA/revision behavior.

- [ ] **Step 8: Commit Task 6**

```bash
git add src/chessgrandmaster/b2/canonicalize.py tests/b2/test_canonicalize.py tests/b2/fixtures
git commit -m "feat: canonicalize and dedupe tournament games"
```

---

### Task 7: Acquisition service from source URL to canonical revision

**Files:**
- Create: `src/chessgrandmaster/b2/acquisition.py`
- Test: `tests/b2/test_acquisition.py`

**Interfaces:**
- Produces: `AcquisitionResult`
- Produces: `AcquisitionService(registry: Registry, store: ObjectStore, workspace: Path, adapters: Mapping[str, SourceAdapter])`
- Produces: `acquire(query: str) -> AcquisitionResult`

- [ ] **Step 1: Write failing end-to-end fixture test**

The test injects a fixture-backed `ChessResultsAdapter`, runs `service.acquire("tnr1450909")`, then asserts:

```text
source row exists
source_tournament external_id == tnr1450909
download_attempt finished successfully
raw source object key contains content SHA
raw object bytes equal downloaded bytes
tournament state == CANONICALIZED
canonical revision exists
canonical PGN parses with python-chess
```

- [ ] **Step 2: Run test and verify failure**

Run: `pytest tests/b2/test_acquisition.py -v`

Expected: FAIL because `AcquisitionService` is missing.

- [ ] **Step 3: Implement acquisition orchestration**

Exact order:

```text
parse/discover SourceRef
-> describe source page
-> upsert source + tournament + source_tournament as DISCOVERED
-> record download_attempt(started)
-> download to workspace temp
-> SHA256 raw bytes
-> LocalObjectStore.put_file(tournaments/<id>/source/<provider>/<sha>/original.pgn)
-> record source_file
-> finish download_attempt(success)
-> state DOWNLOADED
-> parse/validate at least one legal game
-> state VALIDATED
-> canonicalize_source_file(...)
-> state CANONICALIZED
-> return AcquisitionResult
```

On any network/parse failure, finish the download attempt with error text where applicable and do not delete prior successful source files/revisions.

- [ ] **Step 4: Add repeat-ingestion test**

Call `acquire()` twice with identical fixture bytes and assert no duplicate source file, canonical game, occurrence, or revision rows are created. Download-attempt history may append because attempts are historical.

- [ ] **Step 5: Run acquisition tests**

Run: `pytest tests/b2/test_acquisition.py -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 7**

```bash
git add src/chessgrandmaster/b2/acquisition.py tests/b2/test_acquisition.py
git commit -m "feat: add B2 acquisition workflow"
```

---

### Task 8: B2a CLI commands

**Files:**
- Create: `src/chessgrandmaster/b2/cli_acquire.py`
- Create: `src/chessgrandmaster/b2/cli_registry.py`
- Modify: `pyproject.toml`
- Test: `tests/b2/test_b2a_cli.py`

**Interfaces:**
- Produces command: `cgm-acquire`
- Produces command: `cgm-registry`

- [ ] **Step 1: Write failing CLI parser tests**

Required syntax:

```text
cgm-acquire <source-url-or-ref> --registry PATH --root PATH
cgm-registry status [<tournament-id>] --registry PATH
```

Defaults:

```text
--registry .cgm/registry.sqlite
--root .cgm/b2
```

The CLI must print JSON summaries, not scrapeable free-form prose, so the later VPS scheduler can consume it without a second interface.

- [ ] **Step 2: Run CLI tests and verify failure**

Run: `pytest tests/b2/test_b2a_cli.py -v`

Expected: FAIL because entry points/modules are absent.

- [ ] **Step 3: Add entry points**

Modify `pyproject.toml`:

```toml
[project.scripts]
cgm-analyze = "chessgrandmaster.cli:main"
cgm-engine = "chessgrandmaster.engine_manifest:main"
cgm-bench = "chessgrandmaster.benchmark_cli:main"
cgm-acquire = "chessgrandmaster.b2.cli_acquire:main"
cgm-registry = "chessgrandmaster.b2.cli_registry:main"
```

Do not alter existing command behavior.

- [ ] **Step 4: Implement CLIs using existing service/repository interfaces**

`cgm-acquire` constructs `Registry`, `LocalObjectStore`, and `ChessResultsAdapter`; it must not call B1 analysis. `cgm-registry status` reads only `registry.sqlite` and returns state/counts/source provenance.

- [ ] **Step 5: Run CLI and B2 tests**

Run:

```bash
pytest tests/b2 -v
python -m py_compile src/chessgrandmaster/b2/*.py src/chessgrandmaster/b2/sources/*.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 8**

```bash
git add pyproject.toml src/chessgrandmaster/b2/cli_acquire.py src/chessgrandmaster/b2/cli_registry.py tests/b2/test_b2a_cli.py
git commit -m "feat: add B2 acquisition CLI"
```

---

### Task 9: Full regression gate and live Chess-Results acceptance

**Files:**
- No production code should be added unless the acceptance exposes a real adapter defect.
- Update fixture/tests only when a live HTML difference is generalized rather than hard-coded to one tournament.

**Interfaces:**
- Validates all B2a contracts and B1 non-regression.

- [ ] **Step 1: Run the full unit suite**

Run: `pytest -q`

Expected: all existing B1 tests plus new B2a tests PASS; the existing optional/golden skip remains allowed if its environment flag/Stockfish prerequisite is absent.

- [ ] **Step 2: Run static syntax/diff checks**

Run:

```bash
python -m py_compile src/chessgrandmaster/b2/*.py src/chessgrandmaster/b2/sources/*.py
git diff --check
```

Expected: no errors.

- [ ] **Step 3: Install editable package and inspect CLI help**

Run:

```bash
python -m pip install -e .
cgm-acquire --help
cgm-registry --help
```

Expected: both commands resolve.

- [ ] **Step 4: Run one live acquisition against the supplied youth tournament seed**

Run from a disposable B2 workspace:

```bash
CGM_B2_LIVE=1 cgm-acquire \
  'https://s3.chess-results.com/tnr1450909.aspx?lan=1&art=0&turdet=YES&SNode=S0' \
  --registry /tmp/cgm-b2-live/registry.sqlite \
  --root /tmp/cgm-b2-live/objects
```

Acceptance checks:

```text
external_id == tnr1450909
raw PGN stored under SHA-addressed immutable key
at least one legal game canonicalized
canonical PGN reparses with zero parser errors
state == CANONICALIZED
repeat invocation reuses the same canonical revision
```

This is an acceptance check, not a benchmark.

- [ ] **Step 5: Do not broaden scope if sibling age sections are discovered**

Record neighboring `tnr` links as candidate source references if useful, but do not auto-group them into one logical tournament/festival in B2a v1. Any event-family grouping needs a later explicit design because the exact relationship can vary by age, gender, discipline, and time control.

- [ ] **Step 6: Commit only if the live acceptance required generalized adapter changes**

If no code changed, do not create an empty commit. If generalized fixes were necessary:

```bash
git add src/chessgrandmaster/b2/sources/chess_results.py tests/b2
git commit -m "fix: handle live Chess-Results tournament pages"
```

---

## B2a Completion Gate

B2a is complete when all of the following are true:

```text
1. direct Chess-Results source reference resolves deterministically
2. raw PGN is immutable and content-addressed
3. registry.sqlite records source/tournament/file/download provenance
4. legal games receive stable game_fingerprint_v1 identities
5. exact duplicates preserve all occurrences but share one canonical game
6. metadata conflicts are preserved rather than overwritten
7. truncated/prefix games remain separate in v1
8. canonical tournament revision is deterministic and idempotent
9. priority score/reasons are deterministic and do not fake player identity
10. supplied tnr1450909 live source reaches CANONICALIZED
11. all existing B1 tests remain green
```

B2b starts from the stable `tournament_revisions` + `tournament_games` contract produced here; it does not re-parse Internet sources.