# B2b Workload Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a canonical B2a tournament revision into deterministic whole-game shards, provider-neutral `cgm-job-1` documents, and Local/S3-compatible object-store artifacts that B1 can analyze without any chess-semantic changes.

**Architecture:** B2b consumes only `registry.sqlite` canonical revision membership and canonical PGN data created by B2a. A deterministic LPT-style whole-game shard planner balances plies while preserving per-game identity; a packaging service emits one shard PGN and one `JobSpec` per shard, plus a deterministic `TournamentManifest`. Local storage is reused from B2a and S3-compatible storage is added behind the same `ObjectStore` contract; no cloud worker launch, lease, queue, retry, or serverless provider logic is implemented.

**Tech Stack:** Python 3.11+, `python-chess`, stdlib JSON/hash/path utilities, `sqlite3`, optional `boto3` for S3-compatible storage, `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-14-b2-acquisition-workload-design.md`

## Global Constraints

- Work only on `chatgpt-work`; no merge to `main`, PR, force-push, or canonical-upstream changes without explicit approval.
- B1 remains frozen: `PIPELINE_VERSION = 3`, Stockfish 19, depth 19, no time limit, MultiPV 1, snapshots `[12, 14, 16, 18, 19]`, `game_affinity_lpt`, UCI archive enabled.
- `JobSpec` must not contain provider, workers, threads, Hash, local engine path, credentials, or runtime-specific paths.
- Shards contain complete games only; one game never spans shard boundaries.
- Sharding policy version is `whole_game_plies_v1` and initial target is 3,000 plies/shard.
- Packaging is deterministic for the same tournament revision + policy: same shard membership, PGN bytes, SHA256, JobSpec JSON, config hash, and object keys.
- Local and S3 storage use the same logical keys.
- Credentials never enter manifests, JobSpecs, registry rows, PGN, or tests.
- Normal tests use a fake S3 client and no network credentials.
- B3 serverless concerns are out of scope: no provider launch, queue, lease, heartbeat, distributed retry, or auto-scaling.

---

## File Structure

Create:

```text
src/chessgrandmaster/b2/
    manifests.py           # TournamentManifest + JobSpec + analysis contract
    sharding.py            # whole_game_plies_v1 planner
    packaging.py           # canonical revision -> artifacts
    s3_storage.py          # S3ObjectStore implementation
    cli_package.py         # cgm-package

tests/b2/
    test_manifests.py
    test_sharding.py
    test_packaging.py
    test_s3_storage.py
    test_b2b_cli.py
    test_b1_shard_compatibility.py
```

Modify:

```text
src/chessgrandmaster/b2/storage.py   # export shared SHA helper only if needed
src/chessgrandmaster/b2/registry.py  # read APIs + SHARDED/READY transitions
pyproject.toml                       # cgm-package entry point + optional s3 extra
```

---

### Task 1: Provider-neutral analysis contract, TournamentManifest, and JobSpec

**Files:**
- Create: `src/chessgrandmaster/b2/manifests.py`
- Test: `tests/b2/test_manifests.py`

**Interfaces:**
- Produces: `AnalysisContract`
- Produces: `default_b1_v3_analysis_contract() -> AnalysisContract`
- Produces: `TournamentManifest`
- Produces: `JobInput`
- Produces: `JobSpec`
- Produces: `to_canonical_dict()`, `to_canonical_json()`, and hash methods on manifest/spec objects

- [ ] **Step 1: Write failing analysis-contract tests**

```python
from chessgrandmaster.b2.manifests import default_b1_v3_analysis_contract


def test_default_contract_matches_frozen_b1_v3_policy():
    contract = default_b1_v3_analysis_contract()
    assert contract.pipeline_version == 3
    assert contract.engine == "stockfish19"
    assert contract.depth == 19
    assert contract.time_sec == 0
    assert contract.multipv == 1
    assert contract.snapshot_depths == (12, 14, 16, 18, 19)
    assert contract.scheduler == "game_affinity_lpt"
    assert contract.uci_archive is True


def test_analysis_contract_has_no_runtime_resource_fields():
    payload = default_b1_v3_analysis_contract().to_dict()
    forbidden = {"provider", "workers", "threads", "hash_mb", "binary", "engine_path"}
    assert forbidden.isdisjoint(payload)
```

- [ ] **Step 2: Run test and verify failure**

Run: `pytest tests/b2/test_manifests.py -v`

Expected: FAIL because `manifests.py` does not exist.

- [ ] **Step 3: Implement immutable dataclasses**

Use `@dataclass(frozen=True)` and exact fields:

```python
@dataclass(frozen=True)
class AnalysisContract:
    pipeline_version: int
    engine: str
    depth: int
    time_sec: float
    multipv: int
    snapshot_depths: tuple[int, ...]
    scheduler: str
    uci_archive: bool

@dataclass(frozen=True)
class JobInput:
    key: str
    sha256: str
    games: int
    plies: int
    canonical_game_fingerprints: tuple[str, ...]

@dataclass(frozen=True)
class JobSpec:
    schema_version: str
    job_id: str
    tournament_id: str
    tournament_revision: int
    shard_index: int
    input: JobInput
    analysis: AnalysisContract
    config_hash: str
```

`default_b1_v3_analysis_contract()` returns the exact frozen policy above. Keep this function in B2 so B1 does not need to expose a new API solely for packaging; add a test that `production_pipeline.PIPELINE_VERSION == contract.pipeline_version`.

- [ ] **Step 4: Write failing canonical serialization/hash tests**

Construct logically identical `TournamentManifest`/`JobSpec` objects twice and assert byte-for-byte canonical JSON equality. Assert `config_hash` is `canonical_json_sha256(analysis.to_dict())` and is unaffected by object keys or local paths.

- [ ] **Step 5: Implement canonical serialization via `b2.json_codec`**

`TournamentManifest` fields:

```python
schema_version="cgm-tournament-1"
tournament_id: str
revision: int
name: str
sources: tuple[dict, ...]
canonical_pgn: dict
properties: dict
created_at: str
canonicalization_policy: str
sharding_policy: str
```

Use the stable `tournament_revisions.created_at` from B2a; never generate a new timestamp on each packaging rerun.

- [ ] **Step 6: Run manifest tests**

Run: `pytest tests/b2/test_manifests.py -v`

Expected: PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add src/chessgrandmaster/b2/manifests.py tests/b2/test_manifests.py
git commit -m "feat: add B2 workload contracts"
```

---

### Task 2: Deterministic whole-game ply-balanced shard planner

**Files:**
- Create: `src/chessgrandmaster/b2/sharding.py`
- Test: `tests/b2/test_sharding.py`

**Interfaces:**
- Produces: `ShardGame(canonical_game_id, fingerprint, ordinal, ply_count, pgn_text)`
- Produces: `ShardPlan(shard_index, games, total_plies)`
- Produces: `plan_shards(games: Sequence[ShardGame], target_plies: int = 3000) -> tuple[ShardPlan, ...]`

- [ ] **Step 1: Write failing whole-game and deterministic tests**

```python
from chessgrandmaster.b2.sharding import ShardGame, plan_shards


def g(i, plies):
    return ShardGame(i, f"f{i}", i, plies, f"[Event \"G{i}\"]\n\n*\n")


def test_shards_never_split_or_duplicate_games():
    games = [g(1, 1800), g(2, 1700), g(3, 900), g(4, 600)]
    plans = plan_shards(games, target_plies=3000)
    ids = [game.canonical_game_id for p in plans for game in p.games]
    assert sorted(ids) == [1, 2, 3, 4]
    assert len(ids) == len(set(ids))


def test_sharding_is_deterministic():
    games = [g(1, 1800), g(2, 1700), g(3, 900), g(4, 600)]
    assert plan_shards(games, 3000) == plan_shards(games, 3000)
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/b2/test_sharding.py -v`

Expected: FAIL because planner is missing.

- [ ] **Step 3: Implement `whole_game_plies_v1`**

Algorithm:

```text
if no games -> return ()
shard_count = max(1, ceil(sum(plies) / target_plies))
sort assignment candidates by (-ply_count, ordinal, fingerprint)
create shard_count bins with load 0
for each game:
    choose bin with minimum (total_plies, shard_index)
    append whole game
inside each bin sort games back by original ordinal
return bins by shard_index
```

This is deterministic LPT balancing. A single game larger than target remains intact in one shard; target is a balancing goal, not a hard split threshold.

- [ ] **Step 4: Add balance-property tests**

For a synthetic set without a single oversized game, assert max shard load minus min shard load is no greater than the largest game's ply count. Also assert `target_plies <= 0` raises `ValueError`.

- [ ] **Step 5: Run sharding tests**

Run: `pytest tests/b2/test_sharding.py -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/chessgrandmaster/b2/sharding.py tests/b2/test_sharding.py
git commit -m "feat: add whole-game shard planner"
```

---

### Task 3: Registry read API for packaging

**Files:**
- Modify: `src/chessgrandmaster/b2/registry.py`
- Test: `tests/b2/test_registry.py`

**Interfaces:**
- Produces: `get_revision_for_packaging(tournament_id: str, revision_number: int | None = None) -> RevisionPackageSource`
- Produces: `mark_sharded(tournament_id: str, revision_id: int) -> None`
- Produces: `mark_ready(tournament_id: str, revision_id: int) -> None`

- [ ] **Step 1: Write failing package-source test**

Seed one tournament revision with three canonical game memberships and assert returned DTO contains:

```text
tournament id/name/revision/created_at
canonical SHA/policy
ordered source provenance
ordered canonical games with fingerprint, ordinal, ply_count, selected display headers and mainline UCI
priority properties/reasons
```

No raw SQLite rows escape the repository.

- [ ] **Step 2: Run focused test and verify failure**

Run: `pytest tests/b2/test_registry.py -k packaging -v`

Expected: FAIL because read API is missing.

- [ ] **Step 3: Implement read DTOs and state transitions**

`mark_sharded` requires current state in `{CANONICALIZED, SHARDED}`. `mark_ready` requires current state in `{SHARDED, READY}`. Idempotent same-state calls succeed; illegal backwards/jump transitions raise `ValueError`.

- [ ] **Step 4: Run registry tests**

Run: `pytest tests/b2/test_registry.py -v`

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/chessgrandmaster/b2/registry.py tests/b2/test_registry.py
git commit -m "feat: expose canonical revisions for packaging"
```

---

### Task 4: Packaging service for canonical PGN, shards, manifest, and JobSpecs

**Files:**
- Create: `src/chessgrandmaster/b2/packaging.py`
- Test: `tests/b2/test_packaging.py`

**Interfaces:**
- Produces: `PackageResult`
- Produces: `PackageService(registry: Registry, store: ObjectStore, workspace: Path)`
- Produces: `package(tournament_id: str, revision_number: int | None = None, target_plies: int = 3000) -> PackageResult`

- [ ] **Step 1: Write failing packaging fixture test**

Seed a B2a registry revision with four canonical games and a LocalObjectStore. After `package()` assert logical object layout exactly:

```text
tournaments/<id>/revisions/0001/canonical/tournament.pgn
tournaments/<id>/revisions/0001/canonical/manifest.json
tournaments/<id>/revisions/0001/shards/0000/input.pgn
tournaments/<id>/revisions/0001/shards/0000/job.json
...
```

Assert each `job.json` fingerprint list exactly matches games in its `input.pgn` and every canonical fingerprint across shards appears once.

- [ ] **Step 2: Run packaging test and verify failure**

Run: `pytest tests/b2/test_packaging.py -v`

Expected: FAIL because packaging service is absent.

- [ ] **Step 3: Implement deterministic game reconstruction**

Use the B2a selected display headers + normalized initial FEN + ordered UCI moves. Reconstruct each `chess.pgn.Game`, export mainline-only with `StringExporter(headers=True, variations=False, comments=False, columns=None)`, and use exactly two newlines between games.

Do not import or call B1 exporter; B2 packaging produces input PGN, not Mistake/Blunder output.

- [ ] **Step 4: Implement canonical object keys**

Helper functions must produce zero-padded revision/shard components:

```python
revision_prefix("cr-tnr1450909", 1)
# tournaments/cr-tnr1450909/revisions/0001

shard_input_key(..., 3)
# .../shards/0003/input.pgn
```

Job ID format:

```text
<tournament_id>-r<revision:04d>-s<shard_index:04d>
```

- [ ] **Step 5: Implement package write sequence**

Exact order:

```text
load revision DTO
-> reconstruct canonical tournament PGN and verify SHA equals registry revision SHA
-> plan shards
-> write every shard PGN locally
-> SHA every shard PGN
-> create JobInput + JobSpec with config_hash from AnalysisContract only
-> write canonical JSON job files
-> create TournamentManifest with shard summary extension
-> upload/copy all files through ObjectStore
-> stat every object and verify SHA
-> registry.mark_sharded(...)
-> only after all object verification succeeds: registry.mark_ready(...)
-> return PackageResult
```

If any object verification fails, tournament must remain `SHARDED` or `CANONICALIZED`, never falsely `READY`.

- [ ] **Step 6: Add rerun/idempotency test**

Run `package()` twice and assert same object keys, same bytes, same manifest/job hashes, no duplicate registry memberships, state remains `READY`.

- [ ] **Step 7: Run packaging tests**

Run: `pytest tests/b2/test_packaging.py -v`

Expected: PASS.

- [ ] **Step 8: Commit Task 4**

```bash
git add src/chessgrandmaster/b2/packaging.py tests/b2/test_packaging.py
git commit -m "feat: package canonical B1 workloads"
```

---

### Task 5: S3-compatible ObjectStore implementation

**Files:**
- Create: `src/chessgrandmaster/b2/s3_storage.py`
- Modify: `pyproject.toml`
- Test: `tests/b2/test_s3_storage.py`

**Interfaces:**
- Produces: `S3ObjectStore(bucket: str, prefix: str = "", client=None)`
- Implements the B2a `ObjectStore` protocol.

- [ ] **Step 1: Write fake-client tests before adding boto3 dependency**

Create a minimal fake S3 client implementing:

```text
upload_file
head_object
download_file
list_objects_v2
```

Test logical key prefixing, upload/download round trip, stat size/SHA metadata, list pagination behavior, and immutable collision handling.

- [ ] **Step 2: Run test and verify failure**

Run: `pytest tests/b2/test_s3_storage.py -v`

Expected: FAIL because S3ObjectStore is missing.

- [ ] **Step 3: Add optional S3 dependency**

Modify `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = ["pytest>=8"]
s3 = ["boto3>=1.35"]
```

Normal installation remains lightweight; VPS/S3 installs can use `pip install -e '.[s3]'`.

- [ ] **Step 4: Implement S3ObjectStore with injected-client first**

When `client is None`, import `boto3` lazily and call `boto3.client("s3")`, allowing standard AWS/S3-compatible environment/config credential discovery. Do not add access keys/secrets as constructor fields.

On upload, compute local SHA256 and store it as object metadata `cgm-sha256`. If key exists:

```text
same metadata SHA -> reuse
missing/different SHA -> download/verify or reject immutable collision
```

`stat()` returns B2 `ObjectStat`; `list()` handles `ContinuationToken` until complete.

- [ ] **Step 5: Run S3 tests without credentials**

Run: `pytest tests/b2/test_s3_storage.py -v`

Expected: PASS entirely through fake client.

- [ ] **Step 6: Commit Task 5**

```bash
git add pyproject.toml src/chessgrandmaster/b2/s3_storage.py tests/b2/test_s3_storage.py
git commit -m "feat: add S3-compatible B2 storage"
```

---

### Task 6: `cgm-package` CLI

**Files:**
- Create: `src/chessgrandmaster/b2/cli_package.py`
- Modify: `pyproject.toml`
- Test: `tests/b2/test_b2b_cli.py`

**Interfaces:**
- Produces command: `cgm-package <tournament-id>`

- [ ] **Step 1: Write failing CLI parser tests**

Required local form:

```text
cgm-package <tournament-id>
  --registry PATH
  --root PATH
  --revision N
  --target-plies 3000
  --store local
```

Required S3 form:

```text
cgm-package <tournament-id>
  --registry PATH
  --root PATH
  --store s3
  --s3-bucket BUCKET
  --s3-prefix PREFIX
  --s3-endpoint-url URL
```

`--revision` is optional and defaults to latest canonical revision. `--s3-endpoint-url` is passed when constructing the boto3 client; credentials come from standard environment/config, not CLI flags.

- [ ] **Step 2: Run CLI test and verify failure**

Run: `pytest tests/b2/test_b2b_cli.py -v`

Expected: FAIL because CLI is absent.

- [ ] **Step 3: Add `cgm-package` entry point**

```toml
cgm-package = "chessgrandmaster.b2.cli_package:main"
```

Keep every existing script entry unchanged.

- [ ] **Step 4: Implement CLI output as canonical JSON summary**

Success output includes:

```json
{
  "tournament_id": "...",
  "revision": 1,
  "state": "READY",
  "manifest_key": "...",
  "shards": 3,
  "games": 50,
  "plies": 4624
}
```

Return nonzero on invalid state, missing revision, checksum failure, or storage error.

- [ ] **Step 5: Run B2b CLI tests**

Run: `pytest tests/b2/test_b2b_cli.py -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

```bash
git add pyproject.toml src/chessgrandmaster/b2/cli_package.py tests/b2/test_b2b_cli.py
git commit -m "feat: add B2 packaging CLI"
```

---

### Task 7: Prove emitted shard PGN is directly consumable by B1

**Files:**
- Create: `tests/b2/test_b1_shard_compatibility.py`
- No B1 production files should change.

**Interfaces:**
- Consumes: `PackageService` output shard PGN.
- Consumes existing B1: `production_pipeline.import_pgn(pgn_path, db_path)`.

- [ ] **Step 1: Write compatibility test using B1 import only**

```python
from chessgrandmaster.production_pipeline import import_pgn


def test_packaged_shard_imports_into_b1_without_semantic_adapter(packaged_fixture, tmp_path):
    shard_path = packaged_fixture.first_shard_path
    result = import_pgn(shard_path, tmp_path / "analysis.sqlite")
    assert result["games"] == packaged_fixture.first_shard_game_count
    assert result["moves"] == packaged_fixture.first_shard_ply_count
```

Do not invoke Stockfish in this unit test; the contract under test is PGN/input compatibility, not engine performance.

- [ ] **Step 2: Run compatibility test and verify expected result**

Run: `pytest tests/b2/test_b1_shard_compatibility.py -v`

Expected: PASS. If it fails, fix B2 PGN packaging; do not change B1 importer unless the failure proves an independent B1 bug.

- [ ] **Step 3: Add source-order/mapping assertion**

After B1 import, query B1 `games.source_game_index` and assert it corresponds one-to-one with `JobSpec.input.canonical_game_fingerprints` order through the B2 shard plan. This proves later result layers can map an analysis SQLite game back to its canonical identity without embedding provider-specific state into B1.

- [ ] **Step 4: Run B1 compatibility plus all B2 tests**

Run:

```bash
pytest tests/b2 -v
```

Expected: PASS.

- [ ] **Step 5: Commit Task 7**

```bash
git add tests/b2/test_b1_shard_compatibility.py
git commit -m "test: prove B2 shards are B1 compatible"
```

---

### Task 8: Full B2 regression gate and end-to-end local acceptance

**Files:**
- No new production files expected.

- [ ] **Step 1: Run complete project tests**

Run: `pytest -q`

Expected: all B1 + B2 tests PASS; only pre-existing environment-gated optional/golden skip is acceptable.

- [ ] **Step 2: Run syntax and diff checks**

Run:

```bash
python -m py_compile src/chessgrandmaster/b2/*.py src/chessgrandmaster/b2/sources/*.py
git diff --check
```

Expected: no errors.

- [ ] **Step 3: Execute B2a live seed if not already present**

```bash
cgm-acquire \
  'https://s3.chess-results.com/tnr1450909.aspx?lan=1&art=0&turdet=YES&SNode=S0' \
  --registry /tmp/cgm-b2-e2e/registry.sqlite \
  --root /tmp/cgm-b2-e2e/objects
```

Expected: state `CANONICALIZED` and stable revision number.

- [ ] **Step 4: Package the live canonical revision locally**

```bash
cgm-package cr-tnr1450909 \
  --registry /tmp/cgm-b2-e2e/registry.sqlite \
  --root /tmp/cgm-b2-e2e/objects \
  --store local \
  --target-plies 3000
```

Expected: state `READY`; manifest + all shard PGN/job files exist and verify by SHA.

- [ ] **Step 5: Run B1 import smoke check on one emitted shard**

Use a small Python one-liner or existing test helper to call `production_pipeline.import_pgn()` against one `input.pgn`. Expected game/move counts equal the corresponding JobSpec.

Do not run a Stockfish benchmark or full tournament analysis as part of B2 acceptance.

- [ ] **Step 6: Repeat packaging and prove byte-level idempotency**

Hash the first manifest and every `input.pgn`/`job.json`, rerun `cgm-package`, and assert all hashes unchanged.

- [ ] **Step 7: Commit only generalized fixes discovered by live acceptance**

No empty commit. If a real bug is fixed, use a scoped message such as:

```bash
git commit -m "fix: make B2 packaging live-source deterministic"
```

---

## B2b Completion Gate

B2b is complete when:

```text
1. one B2a canonical revision packages deterministically
2. every shard contains whole games only
3. shard balancing uses whole_game_plies_v1 and target ~3000 plies
4. every canonical game in the revision appears in exactly one shard
5. TournamentManifest is deterministic and uses cgm-tournament-1
6. JobSpecs use cgm-job-1 and frozen B1 v3 analysis policy
7. JobSpecs contain no provider/workers/threads/Hash/credentials
8. LocalObjectStore and fake-client S3ObjectStore pass checksum tests
9. registry state advances CANONICALIZED -> SHARDED -> READY only after verification
10. at least one emitted shard imports directly through existing B1 import_pgn
11. supplied youth-tournament seed completes acquire -> package locally
12. all existing B1 tests remain green
```

After this gate, B3 can attach any compute worker to a `READY` JobSpec without redesigning acquisition, dedupe, canonical tournament identity, sharding, or B1.