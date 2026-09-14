# B2 acquisition and workload packaging design

Date: 2026-09-14
Branch: `chatgpt-work`

## Goal

Build the input supply chain around the already validated B1 analyzer without changing B1 chess semantics.

B2 has two parts:

- **B2a — Acquisition / Tournament Registry**: discover tournament sources, download and preserve raw PGN, validate and canonicalize games, deduplicate exact game content across sources, and rank tournaments for processing.
- **B2b — Workload Packaging**: turn canonical tournaments into immutable whole-game shards plus provider-neutral `JobSpec` documents that any future worker can execute with B1.

B3 serverless compute is explicitly deferred. B2 prepares data and portable workloads only.

## Architectural principles

1. **B1 is frozen as the analysis engine.** B2 may call B1, but must not change Lucas classification, engine search policy, worker scheduling, PGN export semantics, or pipeline version for ingestion convenience.
2. **Raw source files are immutable.** A downloaded PGN is never overwritten or deleted because it is a duplicate.
3. **Canonical game identity is global and deduplicated.** Multiple source occurrences may point to one canonical game.
4. **A canonical tournament revision contains each canonical game at most once.** Duplicate source occurrences cannot create duplicate B1 work inside that revision.
5. **Provenance is preserved.** Every canonical game can be traced back to every raw occurrence and source URL.
6. **No fuzzy auto-merge in v1.** Exact chess-content matches may auto-dedupe; metadata similarity, truncation, or conflicting move sequences are preserved for later reconciliation.
7. **Whole-game affinity crosses the B1/B2 boundary.** A game must never be split across shards.
8. **Provider resources are not part of `JobSpec`.** Workers/threads/Hash belong to runtime platform policy, not tournament packaging.
9. **Object storage is abstract.** B2 targets a generic object store with Local and S3-compatible implementations. Cloudflare R2 is only a future backend option.
10. **Cross-tournament analysis reuse is enabled by identity but not required in B2 v1.** The registry records stable canonical game fingerprints and config hashes so a later scheduler/result layer can reuse an existing analysis instead of recomputing the same game. B2 v1 guarantees no duplicate game inside one packaged tournament revision.

## System boundary

```text
Internet sources
    -> source adapters
    -> tournament registry
    -> immutable raw PGN
    -> validation / canonicalization
    -> canonical game registry
    -> tournament revision membership
    -> tournament manifest
    -> whole-game shard planner
    -> shard PGN + JobSpec
    -> local/S3-compatible object store
    -> READY workload for B1
```

B2 does not launch cloud workers, manage leases, heartbeats, retries across providers, or implement a distributed queue. Those belong to B3.

## B2a source adapters

Each source adapter exposes the same conceptual interface:

```python
class SourceAdapter:
    def discover(self, query): ...
    def describe(self, source_ref): ...
    def download_pgn(self, source_ref, destination): ...
```

The first implementation target is Chess-Results because tournament pages can expose large PGN bundles suitable for batch ingestion.

Later adapters may include TWIC, Lichess, Chess.com, federation sites, and organizer sites. Adding a source must not change canonicalization, registry identity, sharding, or B1.

Adapters must identify themselves with a stable provider name, use bounded retries/backoff, and expose enough source metadata to reproduce the download. Network behavior is tested with recorded/static fixtures; the normal unit suite does not depend on live websites.

### Tournament priority

Registry priority is rule-based and explainable. Initial ranking signals, in order of importance:

1. tournament contains a Vietnamese player,
2. OTB / standard / classical event,
3. strong event or useful game volume,
4. downloadable and parseable PGN completeness,
5. source confidence.

The registry stores both the score and reasons used to produce it. Unknown source fields remain unknown rather than being guessed. Vietnamese detection starts with PGN federation headers (`VIE`) and a small alias table, with room for later FIDE identity mapping.

## Registry database

B2 uses a dedicated `registry.sqlite`. It is not an analysis database.

Initial schema concepts:

```text
sources
    id
    name
    base_url
    enabled
    priority


tournaments
    id
    slug
    name
    site
    country
    start_date
    end_date
    time_control_class
    is_otb
    has_vietnamese_player
    priority_score
    priority_reasons_json
    status
    created_at
    updated_at


source_tournaments
    id
    tournament_id
    source_id
    external_id
    source_url
    pgn_url
    discovered_at
    last_seen_at


source_files
    id
    tournament_id
    source_id
    source_url
    object_key
    filename
    sha256
    byte_size
    content_type
    status
    downloaded_at


download_attempts
    id
    source_file_id
    started_at
    finished_at
    http_status
    error


canonical_games
    id
    fingerprint_version
    fingerprint
    variant
    initial_fen
    mainline_uci
    ply_count
    canonical_headers_json
    canonical_occurrence_id
    quality_score
    created_at


game_occurrences
    id
    canonical_game_id
    tournament_id
    source_file_id
    source_game_index
    raw_headers_json
    raw_pgn_object_key
    discovered_at


tournament_revisions
    id
    tournament_id
    revision_number
    canonical_pgn_sha256
    canonicalization_policy_version
    created_at


tournament_games
    tournament_revision_id
    canonical_game_id
    ordinal
    selected_occurrence_id
    UNIQUE(tournament_revision_id, canonical_game_id)
    UNIQUE(tournament_revision_id, ordinal)
```

`tournament_games` is the explicit bridge between global game identity and tournament-local order. The same canonical game may legitimately belong to more than one tournament revision, but it can appear only once inside a single revision.

The implementation may normalize individual columns further, but these relationships are contractual: many occurrences may map to one canonical game, and source history remains queryable.

## Game identity and deduplication

### Exact canonical fingerprint

The automatic dedupe key is based on chess content, not PGN formatting or metadata spelling.

```text
game_fingerprint_v1 = SHA256(
    variant
    + normalized_initial_fen
    + ordered_mainline_uci_moves
)
```

For standard chess, the default initial position is normalized explicitly rather than depending on whether a source emitted a `FEN` header.

The fingerprint does **not** include Event, Round, player spelling, Result, comments, NAGs, or annotations.

Consequences:

- same moves with different comments or headers -> one canonical game, multiple occurrences;
- same players/round but different moves -> separate canonical games;
- conflicting Result headers with identical moves -> one canonical game plus metadata conflict;
- shorter PGN that is a prefix of a longer PGN -> not auto-merged in v1.

### Metadata conflicts

Canonical identity and metadata truth are separate concerns.

If exact duplicate occurrences disagree on Result, player spelling, round, date, or event name, B2 retains all raw values and records the conflict. A source-precedence policy may choose a canonical display value without deleting alternatives.

Initial source precedence may prefer official organizer/federation data, then Chess-Results, then TWIC, then secondary sources, but the raw occurrence remains authoritative evidence of what each source actually supplied.

### Truncated and probable duplicates

B2 v1 may detect probable relations such as same players/event/round with one legal mainline being a prefix of another, but it must not auto-merge them. Such cases are preserved as separate canonical games and may be marked for later reconciliation.

## Raw and canonical PGN policy

Raw PGN is immutable and stored exactly as downloaded.

Canonical PGN is a deterministic projection intended for downstream processing. It must:

- parse as legal PGN with python-chess,
- preserve complete legal mainlines,
- use UTF-8 output,
- normalize required headers deterministically,
- contain each selected canonical game exactly once,
- preserve deterministic tournament-local game order through `tournament_games.ordinal`,
- preserve provenance in the registry and manifest,
- avoid destructive removal of source comments/variations from raw storage.

B1 receives a clean analysis projection; the raw corpus remains richer than the analyzer input.

## Tournament identity and revisions

A source reference such as `chess-results:tnr1483080` identifies a source occurrence, not global tournament truth.

A tournament manifest references one canonical PGN revision by content SHA256. If later ingestion produces materially different canonical chess content, B2 creates a new revision rather than silently overwriting the previous revision.

Only changed canonical game content needs to be considered for future re-analysis. Cross-tournament reuse of an already completed B1 result is intentionally deferred until the later result/scheduler layer, but the canonical fingerprint and analysis `config_hash` make that reuse possible without changing B1.

## TournamentManifest contract

Each canonical tournament revision produces a deterministic JSON document with schema version `cgm-tournament-1`.

Minimum fields:

```json
{
  "schema_version": "cgm-tournament-1",
  "tournament_id": "cr-tnr1483080",
  "revision": 1,
  "name": "Example Tournament",
  "sources": [
    {
      "provider": "chess-results",
      "external_id": "tnr1483080",
      "source_url": "https://example.invalid/source"
    }
  ],
  "canonical_pgn": {
    "key": "tournaments/cr-tnr1483080/revisions/0001/canonical/tournament.pgn",
    "sha256": "example-sha256",
    "games": 184,
    "plies": 17243
  },
  "properties": {
    "is_otb": true,
    "time_control_class": "classical",
    "has_vietnamese_player": true
  },
  "created_at": "2026-09-14T00:00:00Z"
}
```

Manifest JSON serialization is canonical/deterministic so its hash can be used in audits and later distributed execution.

## B2b whole-game sharding

Shards contain complete games only. No game may span shard boundaries.

The first planner balances by total plies, not only by game count. Default target is configurable; the initial operational target is approximately 3,000 plies per shard, with deterministic ordering and packing.

The planner must be deterministic: the same canonical tournament revision plus the same sharding policy produces the same shard boundaries, filenames, hashes, and `JobSpec` documents.

Sharding metadata records canonical game IDs/fingerprints included in each shard so a later result can always be mapped back to the canonical registry.

## JobSpec contract

Each shard produces one provider-neutral `cgm-job-1` document.

```json
{
  "schema_version": "cgm-job-1",
  "job_id": "cr-tnr1483080-r0001-s0003",
  "tournament_id": "cr-tnr1483080",
  "tournament_revision": 1,
  "shard_index": 3,
  "input": {
    "key": "tournaments/cr-tnr1483080/revisions/0001/shards/0003/input.pgn",
    "sha256": "example-sha256",
    "games": 31,
    "plies": 2844,
    "canonical_game_fingerprints": ["fingerprint-a", "fingerprint-b"]
  },
  "analysis": {
    "pipeline_version": 3,
    "engine": "stockfish19",
    "depth": 19,
    "time_sec": 0,
    "multipv": 1,
    "snapshot_depths": [12, 14, 16, 18, 19],
    "scheduler": "game_affinity_lpt",
    "uci_archive": true
  },
  "config_hash": "example-config-hash"
}
```

`JobSpec` intentionally excludes provider, workers, threads, and Hash. Those values are selected by the runtime resource profile when B1 executes the job.

`config_hash` is computed from canonical JSON for the analysis contract, not from mutable local paths.

## Object storage abstraction

B2 defines a narrow object-store boundary:

```python
class ObjectStore:
    def put_file(self, key, path): ...
    def get_file(self, key, destination): ...
    def exists(self, key): ...
    def stat(self, key): ...
    def list(self, prefix): ...
```

Initial implementations:

- `LocalObjectStore` for tests, workstation use, and VPS staging;
- `S3ObjectStore` for the user's existing S3-compatible storage.

Credentials come from environment/configuration and are never written into manifests or JobSpecs.

R2 support later uses the S3-compatible implementation rather than changing B2 contracts.

## Object layout

```text
tournaments/
  <tournament_id>/
    source/
      <provider>/
        <source-file-sha>/
          original.pgn
          metadata.json

    revisions/
      0001/
        canonical/
          tournament.pgn
          manifest.json

        shards/
          0000/
            input.pgn
            job.json
          0001/
            input.pgn
            job.json
          ...

        jobs/
          <job_id>/
            analysis.sqlite
            uci/
            Mistakes.pgn
            Blunders.pgn
            manifest.json
```

B2 creates data through `shards/`. The `jobs/` output layout is reserved now so B3 and later project phases can preserve B1 SQLite, raw engine telemetry, and PGN slices without redesigning storage.

## CLI surface

B2 should introduce separate commands rather than overload `cgm-analyze`.

Initial target commands:

```text
cgm-acquire <source-url-or-ref>
cgm-package <tournament-id>
cgm-registry status [<tournament-id>]
```

`cgm-acquire` performs source discovery/download/validation/registration and may canonicalize the acquired tournament.

`cgm-package` creates or reuses a canonical revision, writes `TournamentManifest`, plans whole-game shards, emits shard PGNs and `JobSpec` files, and uploads through the selected object store.

B1 remains runnable directly as before:

```text
cgm-analyze <shard-input.pgn>
```

No B2 command should require serverless infrastructure.

## State model

B2 registry state is intentionally pre-compute:

```text
DISCOVERED
    -> DOWNLOADED
    -> VALIDATED
    -> CANONICALIZED
    -> SHARDED
    -> READY
```

B2 does not implement `RUNNING`, leases, heartbeats, provider attempts, or distributed retry state. Those are B3 concerns.

## Error handling

- Network/download failures create a `download_attempts` record and do not destroy previous successful raw files.
- Invalid PGN is retained as raw source evidence and marked failed validation.
- A partially invalid file records per-game parse results where possible; invalid games are excluded from canonical workload until reconciled.
- Duplicate raw files are stored/referenced by SHA without creating duplicate canonical games inside a tournament revision.
- Canonicalization and sharding are idempotent for the same source content and policy version.
- Object writes are checksum-verified before registry state advances to READY.

## Versioning

Start with:

- registry schema version: `1`,
- game fingerprint: `game_fingerprint_v1`,
- tournament manifest: `cgm-tournament-1`,
- job spec: `cgm-job-1`,
- canonicalization policy: `canonical_pgn_v1`,
- sharding policy: `whole_game_plies_v1`.

Policy versions are recorded in manifests so future canonicalization or sharding changes can create a new revision without corrupting historical provenance.

## Testing strategy

Implementation follows TDD.

Required test groups:

1. exact game fingerprint is stable across comments, NAGs, whitespace and header spelling differences;
2. different mainline moves do not dedupe even when player/event metadata match;
3. identical moves with conflicting Result headers map to one canonical game while preserving both occurrences;
4. truncated-prefix games are not auto-merged in v1;
5. raw source files remain immutable and addressable after canonicalization;
6. one canonical game may belong to multiple tournament revisions, but appears only once per revision;
7. canonical PGN generation is deterministic and parseable;
8. Vietnamese priority detection recognizes `VIE` federation and records reasons;
9. tournament priority scoring is deterministic and explainable;
10. whole-game sharding never splits a game and is deterministic;
11. sharding balances on plies within policy expectations;
12. `TournamentManifest` and `JobSpec` canonical JSON/hash outputs are deterministic;
13. `JobSpec` contains B1 v3 analysis policy but no provider/workers/threads/Hash;
14. Local object-store round trips and checksum verification pass;
15. S3 object-store behavior is covered through a fake/stub client without requiring network credentials in the normal test suite;
16. an emitted shard PGN can be consumed by the existing B1 import/analyzer pipeline without modifying B1 semantics;
17. source adapters use fixtures in the normal test suite rather than live website dependencies;
18. all existing B1 tests remain green.

## Delivery sequence

### B2.1 — Contracts and identity

Implement canonical JSON utilities, game fingerprinting, `TournamentManifest`, `JobSpec`, object-key helpers, tournament revision membership, and tests.

### B2.2 — Registry

Implement `registry.sqlite` schema, idempotent migrations, source/tournament/file/game-occurrence persistence, metadata conflicts, priority reasons, and revision membership.

### B2.3 — First acquisition adapter

Implement Chess-Results source parsing/download contract and raw immutable storage. Acceptance uses one real tournament source supplied by the user.

### B2.4 — Canonicalization and dedupe

Parse downloaded PGN, compute fingerprints, create canonical games and occurrences, produce a deterministic canonical tournament revision, and apply Vietnamese/OTB priority signals.

### B2.5 — Packaging

Implement deterministic whole-game ply-balanced shards, emit manifests/JobSpecs, write Local/S3 object storage, and prove at least one shard is directly consumable by B1.

Additional source adapters follow after the first end-to-end Chess-Results path works.

## Acceptance criteria

For a real Chess-Results tournament URL, one B2 workflow must be able to produce:

```text
registry.sqlite
immutable raw PGN
canonical deduplicated tournament PGN
TournamentManifest
N whole-game shard PGNs
N provider-neutral JobSpecs
local or S3-compatible object-store copies
```

If the same chess games are later encountered through another source, raw occurrences are preserved and exact duplicate occurrences map to the existing global canonical game. Within any one tournament revision, that canonical game is packaged at most once.

At least one emitted `input.pgn` must run successfully through the existing B1 path without any B1 chess-logic changes.

## Explicit non-goals

- no Modal, Cloud Run, Azure or Cloudflare worker integration;
- no distributed queue;
- no worker leases, fencing or cloud-provider retries;
- no automatic serverless launch;
- no fuzzy duplicate merge;
- no automatic truncated-game merge;
- no cross-tournament B1 result-cache implementation in B2 v1;
- no change to B1 engine/resource policies;
- no benchmark work;
- no post-analysis product logic for Mistake/Blunder slices yet.

The purpose of B2 is one thing: create a durable, deduplicated, provenance-preserving input pipeline that feeds portable B1 workloads and preserves the data contracts needed by later phases.