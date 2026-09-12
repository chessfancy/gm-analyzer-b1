# ChessGrandmaster Analyzer

Portable Stockfish analysis pipeline using Lucas Chess R6-compatible move-evaluation semantics.

## Requirements

Linux x86_64 or arm64, Python 3.11+, curl, tar and sha256sum. No root access or sudo is required.

## Platform setup

```bash
bash scripts/setup_platform.sh
```

The setup creates a project-local `.venv`, installs the package, reads the pinned Stockfish release from `engine.toml`, verifies the release checksum, installs Stockfish into a user-owned path and verifies UCI identity.

Engine discovery order: explicit argument, `CGM_STOCKFISH`, managed install path, then `stockfish` in PATH.

Managed destination: `CGM_ENGINE_INSTALL_PATH`, otherwise `$CGM_HOME/bin/stockfish`, otherwise `~/.local/share/chessgrandmaster/bin/stockfish`.

## Standard compute qualification

```bash
bash scripts/qualify_platform.sh
```

The standard benchmark uses Threads=1, MultiPV=1, Hash=256 MB, fixed 1M nodes, raw D18/D19/D20 and full Lucas D18/D19/D20. Qualification uses full-pipeline P95 <= 3 seconds.

The raw phase clears Stockfish hash between sampled positions. The full Lucas phase uses a persistent engine and includes the post-move second search when required.

## Compare qualification reports

After running the same qualification on multiple machines, compare the JSON reports with:

```bash
cgm-bench compare codespaces.json deepnote.json kaggle.json vps.json
```

`compare` validates that reports use the same schema, Stockfish UCI identity, sample corpus, fixed-work nodes, search configuration, tested depths, P95 target and tournament move count before printing a side-by-side table. Binary SHA256 and local file paths are allowed to differ so x86_64 and arm64 official Stockfish builds can still be compared.

The table reports fixed-work NPS plus full Lucas pipeline P95 for every tested depth. The file stem is used as the platform label, so meaningful names such as `deepnote.json` and `kaggle.json` are recommended.

## Development setup

```bash
bash scripts/setup_dev.sh
```

## Production policy

Portable bootstrap and compute qualification do not change production search policy. Production depth/time/nodes policy will be selected separately after cross-platform benchmark evidence is collected.
