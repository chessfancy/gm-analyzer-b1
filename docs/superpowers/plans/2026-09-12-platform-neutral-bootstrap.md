# Platform-Neutral Bootstrap Implementation Plan

**Goal:** Make ChessGrandmaster setup and compute qualification runnable without sudo or Codespaces-specific paths on Codespaces, Deepnote, Kaggle, and Linux VPS hosts.

## Constraints
- No sudo required.
- No hard-coded /usr/local/bin/stockfish.
- No platform-name conditionals.
- Stockfish version/assets/checksums remain controlled by engine.toml.
- Production analysis limiter policy remains unchanged.
- Existing CGM_STOCKFISH override remains supported.
- CGM_ENGINE_INSTALL_PATH overrides the managed install destination.
- CGM_HOME/bin/stockfish is used when CGM_HOME is configured.
- Otherwise use a user-owned engine directory.
- Standard qualification remains Threads=1, MultiPV=1, Hash=256 MB, fixed 1M nodes, raw D18/D19/D20 and full Lucas D18/D19/D20.
