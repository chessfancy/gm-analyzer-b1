# HANDOFF — gm-analyzer-b1 / ChessGrandmaster

> **Ngày handoff:** 2026-09-14  
> **Ngôn ngữ làm việc:** Tiếng Việt  
> **Repo fork đang làm việc:** `https://github.com/chessfancy/gm-analyzer-b1`  
> **Branch:** `chatgpt-work`  
> **Code baseline ngay trước khi tạo HANDOFF:** `5558aa7bd1223478580585074cfb8a9b7cc76e02` — `feat: add B2 acquisition workflow`  
> **Trạng thái hiện tại:** B1 v3 đã freeze; B2a Task 1–7 đã triển khai; **chuẩn bị vào Task 8 — B2a CLI**.  
> **Lưu ý:** commit chứa riêng file `HANDOFF.md` có thể nằm sau code baseline trên; khi bắt đầu Task 8 phải dùng HEAD mới nhất của `chatgpt-work`.

---

## 1. Mục đích của tài liệu này

Tài liệu này là handoff đầy đủ cho một phiên ChatGPT/Hermes/Codex/model khác tiếp quản dự án mà không cần đọc lại toàn bộ lịch sử hội thoại. Nội dung gồm mục tiêu dự án, kiến trúc B1/B2, commit/invariant quan trọng, workflow Chess-Results đã live-accept, acceptance constants, trạng thái Task 7, yêu cầu Task 8, B2b/discovery/B3 và nguyên tắc cộng tác.

Nếu có xung đột giữa tài liệu này và code/spec hiện tại trong repo, ưu tiên: **spec/design đã commit → code/tests trên HEAD → ruling/invariant ghi rõ ở đây → implementation plan → giả định mới của model**. Không tự ý sửa B1 hoặc đổi architecture chỉ vì thấy một cách “đẹp hơn”.

---

# A. BỨC TRANH TỔNG THỂ

## 2. Mục tiêu dự án

Repo `gm-analyzer-b1` là pipeline phân tích cờ vua cho hệ sinh thái ChessGrandmaster/ChessFancy.

```text
Nguồn PGN trên Internet
        ↓
B2a — Acquisition / Registry
        ↓
Canonical tournament revision
        ↓
B2b — Workload Packaging
        ↓
B1 — Stockfish/Lucas analyzer core
        ↓
analysis.sqlite + UCI raw telemetry + Mistakes.pgn + Blunders.pgn
        ↓
Các phase downstream sau này
```

Người dùng đã nói rõ rằng sau các slice Mistake/Blunder sẽ có phase khác tận dụng **SQLite + engine info**; phase đó chưa được tiết lộ đầy đủ. Vì vậy **không loại bỏ SQLite, raw telemetry hoặc thiết kế lại output dựa trên suy đoán**.

Mục tiêu acquisition ban đầu:

- tự động tìm và tải PGN các ván OTB/on-the-board;
- ưu tiên rất cao các ván có kỳ thủ Việt Nam;
- ưu tiên cờ chậm/classical;
- nguồn: Chess-Results trước, sau đó TWIC, Lichess, Chess.com, federation/organizer sites;
- vận hành lâu dài trên VPS + S3/object storage.

## 3. Roadmap

```text
B1   Analyzer Core                         ✅ DONE / FROZEN
B2a  Acquisition + Tournament Registry     🟡 Task 1–7 done; chuẩn bị Task 8
B2b  Workload Packaging                    ⏭ sau khi B2a đóng
B3   Serverless Compute Fabric             ⏸ deferred (“T5”)
```

Để đạt “tự vận hành tìm PGN”, sau B2a còn phase discovery/scheduler:

```text
Discovery worker → tìm tnr/source mới → score VIE/OTB/classical/volume/source confidence → VPS cron → cgm-acquire
```

B2b không phải discovery; B2b đóng gói canonical corpus thành workload cho B1.

---

# B. B1 ANALYZER CORE — ĐÃ FREEZE

## 4. Trạng thái và semantics

B1 production/frozen. Commit production cuối cùng quan trọng:

`6c9e3662dfbc0412c1c4b95b73eaf8b5d1793cfa` — `fix: ignore progress-only depth events for snapshots`

Production semantics đã chốt:

- Stockfish 19;
- pure depth **D19**, không time cap;
- checkpoints **D12/D14/D16/D18/D19**;
- `PIPELINE_VERSION = 3`;
- MultiPV 1;
- raw UCI `INFO_ALL` archive;
- same-game persistent engine affinity/warm state;
- scheduler `game_affinity_lpt`;
- UCI archive enabled;
- không split một game giữa workers/shards.

Platform resource profiles:

- Molab `4W × 1T × 1536 MB Hash/worker`;
- Codespaces `2W × 1T × 1024 MB`;
- Deepnote `2W × 1T × 768 MB`;
- Kaggle `4W × 1T × 1536 MB`.

Không đưa workers/threads/Hash/provider runtime vào B2b JobSpec.

## 5. B1 acceptance thật

50-game Molab acceptance:

- 50 games;
- 4,624 moves;
- 4,624 primary searches;
- 2,099 post searches;
- 6,723 total searches;
- 0 failed;
- 4 workers/session affinity tốt;
- classification và export hợp lệ.

Acceptance DB: `analysis_50more_twic1659_0c076350.sqlite`  
Source hash: `0c0763501f9dcbcf100054acb08baf8e0e0b848bea96d47dcc6dccd2158f31a9`  
Execution ID: `26331f65-195f-4be6-b5c0-ab62e8a02b8b`

Raw UCI facts:

- 4 gzip archives;
- 155,283 INFO;
- 127,737 score-bearing = `6723 × 19` (đủ D1–D19 mọi search);
- 26,892 info strings = `6723 × 4`;
- 654 currmove/currmovenumber;
- tbhits 0;
- max hashfull 117;
- compressed ~5.23 MiB / 50 games (~107 KiB/game sample-only storage observation).

## 6. Snapshot contamination bug đã xử lý

Bug: progress-only INFO kiểu `depth N currmove ...` có thể tăng depth trong aggregate nhưng giữ score/PV/metrics cũ, làm snapshot stale.

- prefix `b7cf2d5` fix terminal D19 bằng latest complete MultiPV1 info sau stream end;
- `6c9e366...` fix tổng quát: checkpoint chỉ capture khi **event hiện tại có cả depth và score**, vẫn giữ raw currmove archive và fragmented aggregate.

Scan acceptance DB:

- 33,615 snapshots;
- chỉ 10 snapshot currmove-triggered (~0.03%);
- 6 khác CP/PV;
- 4 CP/PV giống nhưng metrics stale;
- không D12/D14 contamination.

Classification/export không bị ảnh hưởng. Old DB repair được offline từ raw gzip, không rerun Stockfish.

## 7. Warm-state/scheduler observations — không benchmark thêm

2,080 cặp `post_move(n)` → `primary(n+1)` cùng FEN/worker/session:

- median D19 nodes `423,694 → 309,792.5` (~25.762% giảm);
- median time `396.5 → 282 ms` (~27.837% giảm).

Static LPT actual engine-time loads: `[579.756,859.431,857.451,782.297] sec`; retrospective dynamic whole-game queue ~9.365% lower makespan. Đây chỉ là operational evidence. **User đã yêu cầu không làm benchmark nữa.**

---

# C. B2 ARCHITECTURE

## 8. Docs authoritative

- `docs/superpowers/specs/2026-09-14-b2-acquisition-workload-design.md`
- `docs/superpowers/plans/2026-09-14-b2a-acquisition-registry.md`
- `docs/superpowers/plans/2026-09-14-b2b-workload-packaging.md`

Docs commits đáng chú ý:

- `599b63a...` design ban đầu;
- `bf2fb9e807d929910196ce8bb42620b3f841742c` clarify membership/dedupe;
- `a046d949...` B2a plan;
- `f27d53360048b7869ead01cae8d29bd73aa159f1` B2b plan.

## 9. Nguyên tắc B2 đã chốt

1. B1 frozen; B2 wrap inputs quanh B1.
2. Raw source immutable; duplicate raw không delete/overwrite.
3. Global canonical game identity dựa trên exact chess content.
4. Nhiều source occurrences → một canonical game.
5. Provenance luôn giữ.
6. Không fuzzy auto-merge v1.
7. Whole game không split giữa shards.
8. JobSpec không chứa provider/workers/threads/Hash.
9. ObjectStore abstraction: Local + S3-compatible.
10. Canonical game có thể thuộc nhiều tournament revisions, tối đa một lần/revision.
11. Cross-tournament reuse của completed B1 analysis deferred; fingerprint/config_hash mở đường sau.

---

# D. B2a DATA MODEL + INVARIANTS

## 10. Registry

`registry.sqlite` riêng, không dùng B1 analysis SQLite.

Tables:

`sources`, `tournaments`, `source_tournaments`, `source_files`, `download_attempts`, `canonical_games`, `game_occurrences`, `game_metadata_conflicts`, `tournament_revisions`, `tournament_games`.

State model:

`DISCOVERED → DOWNLOADED → VALIDATED → CANONICALIZED → SHARDED → READY`

Không có RUNNING/leases/provider state ở B2a/B2b.

## 11. Global canonical identity

Fingerprint v1:

```text
SHA256(variant + normalized_initial_fen + ordered_mainline_uci_moves)
```

Normalized FEN giữ 4 field đầu: piece placement, side-to-move, castling, en-passant; bỏ halfmove/fullmove counters.

Không chứa Event/Round/player spelling/Result/comments/NAGs/variations.

Ruling:

- same mainline + metadata khác → same canonical game, multiple occurrences;
- same metadata nhưng move khác → different canonical game;
- conflicting Result trên identical chess content → one canonical game + conflict record;
- truncated prefix vs full → different canonical games v1.

## 12. Global vs tournament-local occurrence — tuyệt đối không nhầm

`canonical_games.canonical_occurrence_id` = optional **GLOBAL** display/provenance occurrence.

`tournament_games.selected_occurrence_id` = valid occurrence của **đúng tournament revision**, và là headers/provenance dùng cho canonical tournament PGN.

Không để Tournament B metadata leak sang projection của Tournament A.

Selection ordering:

```text
source.priority DESC
source.name ASC
source_file.sha256 ASC
source_game_index ASC
occurrence.id ASC   # final tie-break only
```

## 13. Registry hardening

Commit `6943bfd5b1fa377d473989e62a411c8475b1f349` — `fix: enforce B2 registry provenance invariants`.

Đã chốt:

- `(source_id, external_id)` không auto-reparent sang tournament khác;
- occurrence source_file phải tồn tại và đúng tournament;
- occurrence cũ không reuse cross-tournament;
- selected occurrence phải đúng canonical game + đúng revision tournament + valid;
- `upsert_tournament()` preserve omitted/None metadata;
- sentinel cho explicit `False`/`0`;
- status monotonic, không regress.

## 14. Metadata conflicts

Fields: `Event/Site/Date/Round/White/Black/Result`.

Conflict chỉ khi >1 distinct exact source value. Không normalize player spelling thành “một sự thật”. Serialization deterministic; stale conflict phải xóa được khi distinct values còn 1.

---

# E. B2a TASK HISTORY

## 15. Commit chain

```text
Task 1 — identity:
63b637a6786e53a684dccb2cad2fd2f595d69f66
feat: add B2 canonical game identity

Task 2 — immutable LocalObjectStore:
a10f19ea7ef93b35d760eb361ca4959ff2c2dfd1
feat: add immutable local object store

Task 3 — registry:
24429b74ed8672f19985a9c75788b596625b3695
feat: add B2 tournament registry

Task 4 — priority:
0cc7bfd34f91a57dd1e51edc46bcfe2c2c42972b
feat: add explainable tournament priority

Registry hardening:
6943bfd5b1fa377d473989e62a411c8475b1f349

Task 5 initial adapter:
119684b70f958cddcd98d58853352a36fcf654a6

Task 5 accepted real Chess-Results workflow:
b7de393876f6703662a8b940305c59d5d481707a

Task 6 canonicalization:
549f15544bd0e75cb1861ff84c9afc5d0e31e620

Task 7 AcquisitionService:
5558aa7bd1223478580585074cfb8a9b7cc76e02
```

### Task 1 details

`FINGERPRINT_VERSION="game_fingerprint_v1"`; frozen `GameIdentity`; canonical JSON sorted keys/compact/UTF-8/allow_nan=False.

### Task 2 details

`ObjectStore`: `put_file/get_file/exists/stat/list`. Local store rejects traversal/absolute/windows drive/backslash/NUL; SHA256 verify; sibling temp+fsync; same key+same bytes reuse; same key+different bytes error; sorted list.

Known future hardening: RLock chỉ process-local; multi-process same-key race có thể cần xử lý khi VPS/B3 concurrency thực sự xuất hiện. Không block B2a.

### Task 4 priority weights

```text
PLAYER_VIE            100
EVENT_VIE              60
CLASSICAL              30
OTB                    20
GAME_VOLUME_100        10
GAME_VOLUME_500        10
SOURCE_PRIORITY_SCALE  10
```

Domestic codes `HCM/HNO/DAN/QDO/DON/BNI/BLU` không phải bằng chứng FIDE `VIE`.

---

# F. CHESS-RESULTS — WORKFLOW THẬT ĐÃ ACCEPT

## 16. Golden source

`https://s3.chess-results.com/tnr1450909.aspx?lan=1&art=0&turdet=YES&SNode=S0`

Observed title: `Giải Vô địch Cờ vua trẻ quốc gia năm 2026 Cờ tiêu chuẩn: Nam U07`

Identity:

```text
provider     = chess-results
external_id  = tnr1450909
database key = 1450909
```

Không auto-group sibling tnr IDs.

## 17. Real download workflow

Không cần phụ thuộc “Show tournament details”. Database key derive trực tiếp từ `tnrNNNN`.

Game DB entry point:

`https://s3.chess-results.com/partiesuche.aspx`

Live sequence:

```text
GET /partiesuche.aspx
→ parse search form
→ POST dbkey 1450909
→ parse rounds/game counts/download form
→ POST all rounds 1..9
→ PGN bytes
```

Observed field names:

- db key `ctl00$P1$txt_dbkey`
- from `ctl00$P1$txt_rdvon`
- to `ctl00$P1$txt_rdbis`
- hidden `__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`

Không hard-code field names nếu generic parser discover được. Không hard-code `id=50023`.

Adapter phải reuse cookie-aware session, parse actual form action/method, block cross-origin, support GET/POST, atomic `.part` download, reject HTML/empty/error payload, validate PGN-looking content.

## 18. `tnr1450909` rounds

```text
R1 9
R2 9
R3 9
R4 9
R5 8
R6 8
R7 6
R8 7
R9 7
Total 72
```

Live response:

- HTTP 200;
- Content-Type `text; charset=utf-8`;
- Content-Disposition `attachment; filename= "1450909_1_9.pgn"`;
- bytes 57,438.

## 19. PASS-STRONG browser manual vs adapter

Manual browser download và adapter output **byte-for-byte identical**:

```text
bytes       57,438 == 57,438
SHA256      3047a22472bdc436116cb298e0c267730f96800869809b2a34ad0a52601dc878
parsed      72 == 72
errors      0 == 0
plies       5475 == 5475
unique fp   72 == 72
duplicate   0 == 0
```

Ordered fingerprints identical; headers identical; CRLF cả hai; no BOM; same final CRLF; không first differing byte.

**Task 5 accepted.** Không debug downloader lại nếu không có regression/site change thật.

---

# G. TASK 6 CANONICALIZATION

## 20. Semantics

`src/chessgrandmaster/b2/canonicalize.py` / `canonicalize_source_file()`:

- verify source_file/tournament/path/size/SHA provenance;
- UTF-8 optional BOM;
- sequential parse;
- invalid game lưu evidence occurrence `canonical_game_id=None`, `is_valid=False`, parse_error;
- exact dedupe bằng `identify_game()`;
- preserve raw headers;
- recompute conflict fields;
- global display occurrence + tournament-local occurrence;
- deterministic canonical PGN;
- content hash → create/reuse revision;
- deterministic membership/idempotency.

Canonical PGN:

- headers từ tournament-local selected occurrence;
- chess content từ canonical identity;
- strip comments/NAGs/side variations;
- UTF-8, LF;
- exactly two newlines giữa games, deterministic trailing newline;
- standard/FEN/recognized variants supported;
- reparse rồi `identify_game()` bắt buộc same fingerprint.

## 21. Task 6 live acceptance

Raw baseline:

```text
bytes 57438
sha 3047a22472bdc436116cb298e0c267730f96800869809b2a34ad0a52601dc878
games 72
errors 0
plies 5475
unique fp 72
duplicates 0
```

Canonicalization:

```text
source_game_count 72
valid 72
invalid 0
unique canonical games 72
duplicate_occurrence_count 0
canonical plies 5475
metadata_conflict_count 0
```

Canonical artifact:

```text
bytes 55707
sha e6c38936cad53dfdf4662089271d24c2da12954dd7c7bed1ed7d12e30f434eaf
revision_id 1
revision_number 1
```

Reparse:

- 72 games;
- 0 errors;
- fingerprint matches 72/72;
- revision membership 72;
- selected occurrences đúng tournament 72/72.

Second run idempotent: same revision/SHA/bytes/membership/selected occurrence/conflicts; occurrence count `72→72`.

DB row counts stable:

```text
sources 1
tournaments 1
source_tournaments 1
source_files 1
download_attempts 0
canonical_games 72
game_occurrences 72
game_metadata_conflicts 0
tournament_revisions 1
tournament_games 72
```

**Task 6 accepted.**

---

# H. TASK 7 ACQUISITION SERVICE

## 22. Current code baseline

Commit `5558aa7bd1223478580585074cfb8a9b7cc76e02` — `feat: add B2 acquisition workflow`.

File: `src/chessgrandmaster/b2/acquisition.py`.

Public interfaces:

```python
AcquisitionResult
AcquisitionService(registry, store, workspace, adapters)
AcquisitionService.acquire(query: str) -> AcquisitionResult
```

Result mang source/tournament/source_file/raw SHA+bytes/object key/download attempt/revision/canonical SHA+games+plies/path/status/validation counts.

## 23. Orchestration hiện tại

```text
resolve adapter
→ discover SourceRef
→ describe
→ upsert source
→ upsert tournament DISCOVERED
→ upsert source_tournament
→ record download_attempt
→ adapter.download_pgn()
→ SHA256 + bytes
→ ObjectStore.put_file()
→ record source_file
→ finish attempt success
→ DOWNLOADED
→ validate ≥1 legal game
→ VALIDATED
→ canonicalize_source_file()
→ CANONICALIZED
→ AcquisitionResult
```

Tournament slug: `<provider>-<external_id>`; ví dụ `chess-results-tnr1450909`.

Raw key:

`tournaments/<tournament_id>/source/<provider>/<raw_sha256>/original.pgn`

Canonical workspace path:

`<workspace>/canonical/<tournament_id>/<raw_sha256>.pgn`

Canonical projection chưa publish ObjectStore ở Task 7; raw object + registry revision/membership là source of truth, B2b package sau.

Failure semantics: failed download attempt ghi error; raw evidence không delete nếu validation/canonicalization fail; status monotonic. `finish_download_attempt(..., source_file_id=...)` validate provenance.

Không B1/Stockfish/sharding.

---

# I. TASK 8 — VIỆC TIẾP THEO NGAY BÂY GIỜ

## 24. Goal/files

Expose Task-7 AcquisitionService và Registry qua machine-readable CLI cho người dùng/VPS scheduler sau này.

Create:

- `src/chessgrandmaster/b2/cli_acquire.py`
- `src/chessgrandmaster/b2/cli_registry.py`
- `tests/b2/test_b2a_cli.py`

Modify `pyproject.toml`.

Current scripts trước Task 8:

```toml
cgm-analyze = "chessgrandmaster.cli:main"
cgm-engine = "chessgrandmaster.engine_manifest:main"
cgm-bench = "chessgrandmaster.benchmark_cli:main"
```

Add:

```toml
cgm-acquire = "chessgrandmaster.b2.cli_acquire:main"
cgm-registry = "chessgrandmaster.b2.cli_registry:main"
```

Không đổi existing command behavior.

## 25. CLI syntax/defaults

```text
cgm-acquire <source-url-or-ref> --registry PATH --root PATH
cgm-registry status [<tournament-id>] --registry PATH
```

Defaults:

```text
--registry .cgm/registry.sqlite
--root .cgm/b2
```

`cgm-acquire` dựng `Registry + LocalObjectStore + ChessResultsAdapter + AcquisitionService`. Không B1/Stockfish.

## 26. JSON contract

Success stdout = đúng **một JSON object**, no banners/prose. Concept:

```json
{
  "ok": true,
  "source": {"provider":"chess-results","external_id":"tnr1450909","source_url":"..."},
  "tournament": {"id":1,"status":"CANONICALIZED"},
  "raw": {"source_file_id":1,"object_key":"...","sha256":"...","byte_size":57438},
  "canonical": {"revision_id":1,"revision_number":1,"sha256":"...","game_count":72,"ply_count":5475}
}
```

Expected operational failure = structured JSON `ok:false`, `error.type/message`, nonzero exit; không normal traceback; không leak cookies/hidden form data.

`cgm-registry status` read-only. Global status trả table counts + state counts; tournament-specific trả tournament metadata/priority, source_tournaments, source_files, revisions + membership counts.

CLI không dùng `Registry._connect()`; thêm narrow read APIs + tests nếu cần.

## 27. Task 8 tests/gates

Cover:

- parser `cgm-acquire tnr1450909`;
- default/override paths;
- success JSON/exit0;
- structured failure/nonzero;
- registry global/tournament status;
- unknown tournament failure;
- registry status read-only;
- không invoke B1;
- existing scripts còn nguyên.

Run:

```bash
pytest tests/b2/test_b2a_cli.py -v
pytest tests/b2 -v
pytest -q
python -m compileall -q src tests
git diff --check
python -m pip install -e .
cgm-acquire --help
cgm-registry --help
```

**Không benchmark.**

## 28. Task 8 limited live smoke

```bash
cgm-acquire 'https://s3.chess-results.com/tnr1450909.aspx?lan=1&art=0&turdet=YES&SNode=S0' --registry <temp>/registry.sqlite --root <temp>/b2
```

Expected:

```text
ok true
external_id tnr1450909
raw sha 3047a22472bdc436116cb298e0c267730f96800869809b2a34ad0a52601dc878
raw bytes 57438
canonical sha e6c38936cad53dfdf4662089271d24c2da12954dd7c7bed1ed7d12e30f434eaf
canonical games 72
canonical plies 5475
status CANONICALIZED
```

Then run both registry status commands; valid JSON. Recommended commit: `feat: add B2 acquisition CLI`. Không push/merge nếu user chưa yêu cầu.

---

# J. TASK 9 + B2a COMPLETION

Task 9 là completion gate, không subsystem lớn: full tests; syntax/diff; editable install + CLI help; one live acquire; repeat invocation reuse same revision; không auto-group sibling sections.

B2a complete khi direct source deterministic, raw immutable/content-addressed, provenance đầy đủ, stable fingerprints, exact dedupe, conflicts preserved, prefix separate, canonical revision deterministic/idempotent, priority không fake identity, live seed tới CANONICALIZED, B1 regression green.

---

# K. B2b WORKLOAD PACKAGING

Plan: `docs/superpowers/plans/2026-09-14-b2b-workload-packaging.md`.

Tasks đã thiết kế:

1. `AnalysisContract + TournamentManifest + JobSpec`;
2. deterministic whole-game ply sharding;
3. package canonical revision artifacts;
4. LocalObjectStore integration/status;
5. S3ObjectStore fake-client tests;
6. `cgm-package`;
7. prove shard consumable trực tiếp bởi existing B1 `import_pgn()`;
8. full regression/acceptance.

Initial shard target ~3000 plies, deterministic LPT-style, whole-game only.

JobSpec `cgm-job-1`:

```text
pipeline_version 3
stockfish19
depth 19
time_sec 0
multipv 1
snapshot_depths 12,14,16,18,19
scheduler game_affinity_lpt
uci_archive true
```

Không provider/workers/threads/Hash/credentials/runtime paths. TournamentManifest `cgm-tournament-1`.

Reserved layout:

```text
tournaments/<id>/source/...
revisions/0001/canonical/...
revisions/0001/shards/0000/input.pgn
revisions/0001/shards/0000/job.json
jobs/<job_id>/analysis.sqlite
jobs/<job_id>/uci/...
jobs/<job_id>/Mistakes.pgn
jobs/<job_id>/Blunders.pgn
jobs/<job_id>/manifest.json
```

Không merge away SQLite.

---

# L. DISCOVERY + TỰ VẬN HÀNH SAU B2a

Chess-Results là source đầu tiên. Priority intent:

1. Vietnamese player;
2. OTB;
3. classical;
4. strength/useful volume;
5. complete parseable PGN;
6. source confidence.

User observation quan trọng: giải trẻ quốc gia Việt Nam lớn không nhiều, nhưng kỳ thủ trẻ Việt Nam đi SEA/Asian/World youth events thường xuyên. Discovery sau nên tìm **player-level `FED=VIE` ở international events**. Domestic FED column có thể là tỉnh/đơn vị HCM/HNO/DAN/QDO; không coi là VIE.

Target VPS:

```text
cron/systemd timer → Chess-Results discovery → registry candidates → score → new/high-priority tnr → cgm-acquire → raw immutable PGN → canonical revision
```

Sau B2b: `canonical revision → cgm-package → whole-game shards → B1/future compute`.

---

# M. B3 FUTURE COMPUTE FABRIC — DEFERRED

User coi đây là “T5”. Concept đã bàn:

- portable `cloud_worker.py`;
- object store source of truth;
- control DB state machine + queue pointers;
- provider có thể Modal → Cloud Run → Azure;
- Cloudflare control plane sau;
- generic S3-compatible ObjectStore, không hard-code R2;
- ưu tiên VPS + S3 hiện có;
- whole-game shards;
- future leases/fencing;
- segmented UCI archive rotation.

Không thêm RUNNING/lease/provider fields vào B2a registry chỉ để “chuẩn bị B3”.

---

# N. CONSTRAINTS CỨNG

- Không merge upstream/canonical `main` nếu user chưa approve rõ ràng.
- Không PR nếu user chưa yêu cầu.
- Không force-push.
- Không sửa B1 khi làm B2 nếu không explicit approval.
- Không benchmark thêm.
- Không Stockfish trong Task 8.
- Không auto-group sibling tnr IDs.
- Không fuzzy dedupe v1.
- Không dùng metadata làm canonical game identity.
- Không coi HCM/HNO/DAN/QDO là VIE.
- Không hard-code Chess-Results `id=50023`.
- Không hard-code form fields nếu parser discover được.
- Không bỏ immutable raw provenance.
- Không để global headers leak sang tournament-local projection.
- Không đưa workers/threads/Hash/provider vào JobSpec.
- Không split game giữa shards.
- Không loại bỏ SQLite/raw UCI archive.

---

# O. CÁCH CỘNG TÁC VỚI USER

User thích tiếng Việt, technical informal, gọi “bro”; dùng Hermes/Luna Ultra để execute prompt; gửi report + SHA để audit; thích prompt lớn/coherent/copy-paste; làm trên Windows/repo local; muốn audit remote commit thật; thường push sau audit/pass; không muốn benchmark nữa.

Model tiếp quản nên:

1. kiểm tra remote HEAD trước prompt tiếp;
2. đọc spec/plan;
3. audit commit thật, không chỉ tin report;
4. giữ task boundary nhỏ;
5. fixture tests + full regression;
6. live acceptance chỉ một source cụ thể khi cần;
7. không broad crawl trong acceptance;
8. sau mỗi task STOP để review.

---

# P. CHECKPOINT CHO CHAT MỚI

Repo `chessfancy/gm-analyzer-b1`, branch `chatgpt-work`. Code baseline trước HANDOFF: `5558aa7bd1223478580585074cfb8a9b7cc76e02`. Nếu HEAD mới hơn chỉ vì docs handoff, bình thường.

Đọc theo thứ tự:

1. `HANDOFF.md`
2. B2 design spec
3. B2a plan
4. `src/chessgrandmaster/b2/acquisition.py`
5. `src/chessgrandmaster/b2/registry.py`
6. `pyproject.toml`

**Next: B2a Task 8 — CLI**. Sau đó audit → Task 9 → đóng B2a → B2b hoặc discovery worker tùy user chọn.

---

# Q. QUICK REFERENCE

Golden live source:

```text
URL:
https://s3.chess-results.com/tnr1450909.aspx?lan=1&art=0&turdet=YES&SNode=S0

external_id: tnr1450909
database key: 1450909

RAW:
bytes 57438
sha256 3047a22472bdc436116cb298e0c267730f96800869809b2a34ad0a52601dc878
games 72
plies 5475
errors 0

CANONICAL:
bytes 55707
sha256 e6c38936cad53dfdf4662089271d24c2da12954dd7c7bed1ed7d12e30f434eaf
games 72
plies 5475
errors 0
fingerprint re-identification 72/72
```

Important commits:

```text
B1 frozen: 6c9e3662dfbc0412c1c4b95b73eaf8b5d1793cfa
Task1: 63b637a6786e53a684dccb2cad2fd2f595d69f66
Task2: a10f19ea7ef93b35d760eb361ca4959ff2c2dfd1
Task3: 24429b74ed8672f19985a9c75788b596625b3695
Task4: 0cc7bfd34f91a57dd1e51edc46bcfe2c2c42972b
Registry hardening: 6943bfd5b1fa377d473989e62a411c8475b1f349
Task5 initial: 119684b70f958cddcd98d58853352a36fcf654a6
Task5 accepted: b7de393876f6703662a8b940305c59d5d481707a
Task6: 549f15544bd0e75cb1861ff84c9afc5d0e31e620
Task7: 5558aa7bd1223478580585074cfb8a9b7cc76e02
```

---

# R. KẾT LUẬN

Hệ thống không còn ở giai đoạn thử nghiệm ý tưởng acquisition. Đã chứng minh thật:

```text
Chess-Results public forms
→ raw PGN byte-identical với browser manual download
→ exact canonicalization
→ 72/72 identity preserved
→ idempotent tournament revision
```

Task 7 đã nối các thành phần thành `AcquisitionService`. Việc tiếp theo là Task 8 để expose CLI machine-readable, sau đó Task 9 đóng B2a.

Một model mới **không nên quay lại thiết kế lại Task 1–7**. Coi chúng là accepted contracts, audit HEAD hiện tại, và tiếp tục từ Task 8.
