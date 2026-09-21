"""Provider-neutral deterministic B2b manifest and JobSpec documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .json_codec import canonical_json_bytes, canonical_json_sha256


TOURNAMENT_MANIFEST_SCHEMA = "cgm-tournament-1"
JOB_SPEC_SCHEMA = "cgm-job-1"


def _json_ready(value: object) -> object:
    """Return a JSON-compatible copy without changing semantic values."""
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


@dataclass(frozen=True)
class AnalysisContract:
    """The frozen B1 analysis policy carried by every portable job."""

    pipeline_version: int
    engine: str
    depth: int
    time_sec: float
    multipv: int
    snapshot_depths: tuple[int, ...]
    scheduler: str
    uci_archive: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_depths", tuple(int(value) for value in self.snapshot_depths))
        if int(self.pipeline_version) < 1:
            raise ValueError("pipeline_version must be positive")
        if not self.engine:
            raise ValueError("engine must not be empty")
        if int(self.depth) <= 0:
            raise ValueError("depth must be positive")
        if float(self.time_sec) < 0:
            raise ValueError("time_sec must not be negative")
        if int(self.multipv) <= 0:
            raise ValueError("multipv must be positive")
        if not self.snapshot_depths or any(depth <= 0 for depth in self.snapshot_depths):
            raise ValueError("snapshot_depths must contain positive depths")
        if not self.scheduler:
            raise ValueError("scheduler must not be empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "pipeline_version": int(self.pipeline_version),
            "engine": str(self.engine),
            "depth": int(self.depth),
            "time_sec": self.time_sec,
            "multipv": int(self.multipv),
            "snapshot_depths": list(self.snapshot_depths),
            "scheduler": str(self.scheduler),
            "uci_archive": bool(self.uci_archive),
        }

    def to_canonical_dict(self) -> dict[str, object]:
        return self.to_dict()

    def to_canonical_json(self) -> str:
        return canonical_json_bytes(self.to_dict()).decode("utf-8")

    def canonical_sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())

    def config_hash(self) -> str:
        """Return the hash used by JobSpec, independent of runtime resources."""
        return self.canonical_sha256()

    sha256 = canonical_sha256
    hash = canonical_sha256


def default_b1_v3_analysis_contract() -> AnalysisContract:
    """Return the immutable analysis contract shared with B1 production policy."""
    return AnalysisContract(
        pipeline_version=3,
        engine="stockfish19",
        depth=19,
        time_sec=0,
        multipv=1,
        snapshot_depths=(12, 14, 16, 18, 19),
        scheduler="game_affinity_lpt",
        uci_archive=True,
    )


@dataclass(frozen=True)
class JobInput:
    """The portable input object referenced by one JobSpec."""

    key: str
    sha256: str
    games: int
    plies: int
    canonical_game_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_game_fingerprints",
            tuple(str(value) for value in self.canonical_game_fingerprints),
        )
        if not self.key:
            raise ValueError("JobInput key must not be empty")
        if not self.sha256:
            raise ValueError("JobInput sha256 must not be empty")
        if int(self.games) < 0 or int(self.plies) < 0:
            raise ValueError("JobInput counts must not be negative")
        if int(self.games) != len(self.canonical_game_fingerprints):
            raise ValueError("JobInput games must match its fingerprint count")

    def to_dict(self) -> dict[str, object]:
        return {
            "key": str(self.key),
            "sha256": str(self.sha256),
            "games": int(self.games),
            "plies": int(self.plies),
            "canonical_game_fingerprints": list(self.canonical_game_fingerprints),
        }

    def to_canonical_dict(self) -> dict[str, object]:
        return self.to_dict()


@dataclass(frozen=True)
class JobSpec:
    """A provider-neutral, content-addressable B1 workload description."""

    schema_version: str
    job_id: str
    tournament_id: str
    tournament_revision: int
    shard_index: int
    input: JobInput
    analysis: AnalysisContract
    config_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != JOB_SPEC_SCHEMA:
            raise ValueError(f"unsupported JobSpec schema: {self.schema_version!r}")
        if not self.job_id or not self.tournament_id:
            raise ValueError("JobSpec identity fields must not be empty")
        if int(self.tournament_revision) < 1:
            raise ValueError("tournament_revision must be positive")
        if int(self.shard_index) < 0:
            raise ValueError("shard_index must not be negative")
        expected = self.analysis.config_hash()
        if not self.config_hash:
            object.__setattr__(self, "config_hash", expected)
        elif self.config_hash != expected:
            raise ValueError("config_hash must match the AnalysisContract")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": str(self.schema_version),
            "job_id": str(self.job_id),
            "tournament_id": str(self.tournament_id),
            "tournament_revision": int(self.tournament_revision),
            "shard_index": int(self.shard_index),
            "input": self.input.to_dict(),
            "analysis": self.analysis.to_dict(),
            "config_hash": str(self.config_hash),
        }

    def to_canonical_dict(self) -> dict[str, object]:
        return self.to_dict()

    def to_canonical_json(self) -> str:
        return canonical_json_bytes(self.to_dict()).decode("utf-8")

    def canonical_sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())

    sha256 = canonical_sha256
    hash = canonical_sha256


@dataclass(frozen=True)
class TournamentManifest:
    """The deterministic manifest for one canonical tournament revision."""

    schema_version: str
    tournament_id: str
    revision: int
    name: str
    sources: tuple[dict[str, object], ...]
    canonical_pgn: dict[str, object]
    properties: dict[str, object]
    created_at: str
    canonicalization_policy: str
    sharding_policy: str
    shards: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != TOURNAMENT_MANIFEST_SCHEMA:
            raise ValueError(
                f"unsupported TournamentManifest schema: {self.schema_version!r}"
            )
        if not self.tournament_id:
            raise ValueError("tournament_id must not be empty")
        if int(self.revision) < 1:
            raise ValueError("revision must be positive")
        object.__setattr__(
            self,
            "sources",
            tuple(dict(_json_ready(source)) for source in self.sources),
        )
        object.__setattr__(self, "canonical_pgn", dict(_json_ready(self.canonical_pgn)))
        object.__setattr__(self, "properties", dict(_json_ready(self.properties)))
        object.__setattr__(
            self,
            "shards",
            tuple(dict(_json_ready(shard)) for shard in self.shards),
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": str(self.schema_version),
            "tournament_id": str(self.tournament_id),
            "revision": int(self.revision),
            "name": str(self.name),
            "sources": [_json_ready(source) for source in self.sources],
            "canonical_pgn": _json_ready(self.canonical_pgn),
            "properties": _json_ready(self.properties),
            "created_at": str(self.created_at),
            "canonicalization_policy": str(self.canonicalization_policy),
            "sharding_policy": str(self.sharding_policy),
        }
        if self.shards:
            payload["shards"] = [_json_ready(shard) for shard in self.shards]
        return payload

    def to_canonical_dict(self) -> dict[str, object]:
        return self.to_dict()

    def to_canonical_json(self) -> str:
        return canonical_json_bytes(self.to_dict()).decode("utf-8")

    def canonical_sha256(self) -> str:
        return canonical_json_sha256(self.to_dict())

    sha256 = canonical_sha256
    hash = canonical_sha256
