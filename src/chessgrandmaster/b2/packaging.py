"""Deterministic local B2b packaging from registry revisions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Iterable

from .canonicalize import _export_projection
from .identity import GameIdentity
from .json_codec import canonical_json_bytes
from .manifests import (
    JobInput,
    JobSpec,
    TournamentManifest,
    default_b1_v3_analysis_contract,
)
from .registry import Registry, RevisionGame, RevisionPackageSource
from .sharding import SHARDING_POLICY, ShardGame, ShardPlan, plan_shards
from .storage import ObjectStat, ObjectStore, sha256_file


DEFAULT_TARGET_PLIES = 3000


def revision_prefix(tournament_id: str, revision: int) -> str:
    if not tournament_id:
        raise ValueError("tournament_id must not be empty")
    if int(revision) < 1:
        raise ValueError("revision must be positive")
    return f"tournaments/{tournament_id}/revisions/{int(revision):04d}"


def canonical_pgn_key(tournament_id: str, revision: int) -> str:
    return f"{revision_prefix(tournament_id, revision)}/canonical/tournament.pgn"


def canonical_manifest_key(tournament_id: str, revision: int) -> str:
    return f"{revision_prefix(tournament_id, revision)}/canonical/manifest.json"


def shard_prefix(tournament_id: str, revision: int, shard_index: int) -> str:
    if int(shard_index) < 0:
        raise ValueError("shard_index must not be negative")
    return f"{revision_prefix(tournament_id, revision)}/shards/{int(shard_index):04d}"


def shard_input_key(tournament_id: str, revision: int, shard_index: int) -> str:
    return f"{shard_prefix(tournament_id, revision, shard_index)}/input.pgn"


def shard_job_key(tournament_id: str, revision: int, shard_index: int) -> str:
    return f"{shard_prefix(tournament_id, revision, shard_index)}/job.json"


def shard_manifest_key(tournament_id: str, revision: int, shard_index: int) -> str:
    return f"{shard_prefix(tournament_id, revision, shard_index)}/manifest.json"


def shard_checksums_key(tournament_id: str, revision: int, shard_index: int) -> str:
    return f"{shard_prefix(tournament_id, revision, shard_index)}/checksums.json"


def make_job_id(tournament_id: str, revision: int, shard_index: int) -> str:
    return f"{tournament_id}-r{int(revision):04d}-s{int(shard_index):04d}"


def _write_bytes(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _game_projection(game: RevisionGame) -> str:
    identity = GameIdentity(
        fingerprint_version=game.fingerprint_version,
        fingerprint=game.fingerprint,
        variant=game.variant,
        initial_fen=game.initial_fen,
        mainline_uci=game.mainline_uci,
        ply_count=game.ply_count,
    )
    return _export_projection(identity, game.selected_headers)


def _pgn_bytes(games: Iterable[str]) -> bytes:
    projections = tuple(games)
    if not projections:
        raise ValueError("cannot package an empty PGN")
    return ("\n\n".join(projections) + "\n").encode("utf-8")


@dataclass(frozen=True)
class ShardPackage:
    shard_index: int
    job_id: str
    input_key: str
    job_key: str
    manifest_key: str
    checksums_key: str
    input_path: Path
    job_path: Path
    manifest_path: Path
    checksums_path: Path
    input_sha256: str
    job_sha256: str
    games: int
    plies: int
    fingerprints: tuple[str, ...]
    job_spec: JobSpec

    @property
    def input_pgn_key(self) -> str:
        return self.input_key

    @property
    def canonical_game_fingerprints(self) -> tuple[str, ...]:
        return self.fingerprints


@dataclass(frozen=True)
class PackageResult:
    tournament_id: str
    revision: int
    revision_id: int
    state: str
    manifest_key: str
    canonical_pgn_key: str
    manifest_sha256: str
    shards: tuple[ShardPackage, ...]
    games: int
    plies: int
    object_keys: tuple[str, ...]

    @property
    def shard_count(self) -> int:
        return len(self.shards)

    @property
    def game_count(self) -> int:
        return self.games

    @property
    def ply_count(self) -> int:
        return self.plies

    @property
    def first_shard_path(self) -> Path:
        if not self.shards:
            raise ValueError("package contains no shards")
        return self.shards[0].input_path

    def to_summary(self) -> dict[str, object]:
        return {
            "tournament_id": self.tournament_id,
            "revision": self.revision,
            "state": self.state,
            "manifest_key": self.manifest_key,
            "shards": self.shard_count,
            "games": self.games,
            "plies": self.plies,
        }


class PackageService:
    """Build immutable B2b objects through a Local/S3-compatible ObjectStore."""

    def __init__(self, registry: Registry, store: ObjectStore, workspace: Path):
        self.registry = registry
        self.store = store
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _put_and_verify(self, key: str, path: Path) -> ObjectStat:
        expected_size = path.stat().st_size
        expected_sha256 = sha256_file(path)
        stored = self.store.put_file(key, path)
        if (
            stored.size != expected_size
            or stored.sha256 != expected_sha256
            or stored.key != key
        ):
            raise ValueError(f"object checksum verification failed for {key}")
        verified = self.store.stat(key)
        if verified != stored:
            raise ValueError(f"object changed during verification for {key}")
        return verified

    @staticmethod
    def _properties(source: RevisionPackageSource) -> dict[str, object]:
        return {
            "site": source.site,
            "country": source.country,
            "start_date": source.start_date,
            "end_date": source.end_date,
            "time_control_class": source.time_control_class,
            "is_otb": source.is_otb,
            "has_vietnamese_player": source.has_vietnamese_player,
            "priority_score": source.priority_score,
            "priority_reasons": list(source.priority_reasons),
        }

    def package(
        self,
        tournament_id: str | int,
        revision_number: int | None = None,
        target_plies: int = DEFAULT_TARGET_PLIES,
    ) -> PackageResult:
        """Package one canonical revision and advance it only after verification."""
        source = self.registry.get_revision_for_packaging(tournament_id, revision_number)
        if source.state not in {"CANONICALIZED", "SHARDED", "READY"}:
            raise ValueError(
                "packaging requires tournament state CANONICALIZED, SHARDED, or READY"
            )
        if not source.games:
            raise ValueError("cannot package a revision with no canonical games")

        revision_root = self.workspace / source.tournament_id / f"{source.revision_number:04d}"
        canonical_dir = revision_root / "canonical"
        canonical_path = canonical_dir / "tournament.pgn"
        game_projections = tuple(_game_projection(game) for game in source.games)
        canonical_bytes = _pgn_bytes(game_projections)
        # Hashing the actual bytes, rather than JSON, is the registry contract.
        canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
        if canonical_sha256 != source.canonical_sha256:
            raise ValueError(
                "reconstructed canonical PGN SHA256 does not match registry revision"
            )
        _write_bytes(canonical_path, canonical_bytes)

        shard_games = tuple(
            ShardGame(
                canonical_game_id=game.canonical_game_id,
                fingerprint=game.fingerprint,
                ordinal=game.ordinal,
                ply_count=game.ply_count,
                pgn_text=projection,
            )
            for game, projection in zip(source.games, game_projections)
        )
        plans = plan_shards(shard_games, target_plies=target_plies)
        analysis = default_b1_v3_analysis_contract()
        canonical_key = canonical_pgn_key(source.tournament_id, source.revision_number)
        manifest_key = canonical_manifest_key(source.tournament_id, source.revision_number)

        shard_inputs: list[tuple[ShardPlan, Path, bytes, str]] = []
        shard_jobs: list[tuple[ShardPlan, Path, bytes, JobSpec]] = []
        for plan in plans:
            input_bytes = _pgn_bytes(game.pgn_text for game in plan.games)
            input_sha256 = hashlib.sha256(input_bytes).hexdigest()
            input_path = revision_root / "shards" / f"{plan.shard_index:04d}" / "input.pgn"
            _write_bytes(input_path, input_bytes)
            shard_inputs.append((plan, input_path, input_bytes, input_sha256))

            input_document = JobInput(
                key=shard_input_key(
                    source.tournament_id,
                    source.revision_number,
                    plan.shard_index,
                ),
                sha256=input_sha256,
                games=len(plan.games),
                plies=plan.total_plies,
                canonical_game_fingerprints=tuple(
                    game.fingerprint for game in plan.games
                ),
            )
            spec = JobSpec(
                schema_version="cgm-job-1",
                job_id=make_job_id(
                    source.tournament_id,
                    source.revision_number,
                    plan.shard_index,
                ),
                tournament_id=source.tournament_id,
                tournament_revision=source.revision_number,
                shard_index=plan.shard_index,
                input=input_document,
                analysis=analysis,
            )
            job_bytes = spec.to_canonical_json().encode("utf-8")
            job_path = revision_root / "shards" / f"{plan.shard_index:04d}" / "job.json"
            _write_bytes(job_path, job_bytes)
            shard_jobs.append((plan, job_path, job_bytes, spec))

        shard_summaries = tuple(
            {
                "shard_index": plan.shard_index,
                "job_id": spec.job_id,
                "input": spec.input.to_dict(),
                "job_key": shard_job_key(
                    source.tournament_id,
                    source.revision_number,
                    plan.shard_index,
                ),
                "manifest_key": shard_manifest_key(
                    source.tournament_id,
                    source.revision_number,
                    plan.shard_index,
                ),
                "checksums_key": shard_checksums_key(
                    source.tournament_id,
                    source.revision_number,
                    plan.shard_index,
                ),
            }
            for (plan, _job_path, _job_bytes, spec) in shard_jobs
        )
        manifest = TournamentManifest(
            schema_version="cgm-tournament-1",
            tournament_id=source.tournament_id,
            revision=source.revision_number,
            name=source.name,
            sources=tuple(source_item.to_dict() for source_item in source.sources),
            canonical_pgn={
                "key": canonical_key,
                "sha256": canonical_sha256,
                "games": len(source.games),
                "plies": sum(game.ply_count for game in source.games),
            },
            properties=self._properties(source),
            created_at=source.created_at,
            canonicalization_policy=source.canonicalization_policy,
            sharding_policy=SHARDING_POLICY,
            shards=shard_summaries,
        )
        manifest_bytes = manifest.to_canonical_json().encode("utf-8")
        manifest_path = _write_bytes(canonical_dir / "manifest.json", manifest_bytes)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

        shard_artifacts: list[ShardPackage] = []
        for (plan, input_path, _input_bytes, input_sha256), (
            _job_plan,
            job_path,
            job_bytes,
            spec,
        ) in zip(shard_inputs, shard_jobs):
            shard_dir = revision_root / "shards" / f"{plan.shard_index:04d}"
            shard_manifest_path = _write_bytes(shard_dir / "manifest.json", manifest_bytes)
            job_sha256 = hashlib.sha256(job_bytes).hexdigest()
            checksums = {
                "schema_version": "cgm-checksums-1",
                "files": {
                    "input.pgn": input_sha256,
                    "job.json": job_sha256,
                    "manifest.json": manifest_sha256,
                },
            }
            checksums_path = _write_bytes(
                shard_dir / "checksums.json",
                canonical_json_bytes(checksums),
            )
            shard_artifacts.append(
                ShardPackage(
                    shard_index=plan.shard_index,
                    job_id=spec.job_id,
                    input_key=spec.input.key,
                    job_key=shard_job_key(
                        source.tournament_id,
                        source.revision_number,
                        plan.shard_index,
                    ),
                    manifest_key=shard_manifest_key(
                        source.tournament_id,
                        source.revision_number,
                        plan.shard_index,
                    ),
                    checksums_key=shard_checksums_key(
                        source.tournament_id,
                        source.revision_number,
                        plan.shard_index,
                    ),
                    input_path=input_path,
                    job_path=job_path,
                    manifest_path=shard_manifest_path,
                    checksums_path=checksums_path,
                    input_sha256=input_sha256,
                    job_sha256=job_sha256,
                    games=len(plan.games),
                    plies=plan.total_plies,
                    fingerprints=tuple(game.fingerprint for game in plan.games),
                    job_spec=spec,
                )
            )

        object_paths: list[tuple[str, Path]] = [
            (canonical_key, canonical_path),
            (manifest_key, manifest_path),
        ]
        for artifact in shard_artifacts:
            object_paths.extend(
                [
                    (artifact.input_key, artifact.input_path),
                    (artifact.job_key, artifact.job_path),
                    (artifact.manifest_key, artifact.manifest_path),
                    (artifact.checksums_key, artifact.checksums_path),
                ]
            )
        stored_keys: list[str] = []
        for key, path in object_paths:
            self._put_and_verify(key, path)
            stored_keys.append(key)

        if source.state == "CANONICALIZED":
            self.registry.mark_sharded(source.tournament_id, source.revision_id)
            self.registry.mark_ready(source.tournament_id, source.revision_id)
        elif source.state == "SHARDED":
            self.registry.mark_ready(source.tournament_id, source.revision_id)
        current_state = self.registry.get_tournament_status(source.registry_tournament_id)
        return PackageResult(
            tournament_id=source.tournament_id,
            revision=source.revision_number,
            revision_id=source.revision_id,
            state=current_state,
            manifest_key=manifest_key,
            canonical_pgn_key=canonical_key,
            manifest_sha256=manifest_sha256,
            shards=tuple(shard_artifacts),
            games=len(source.games),
            plies=sum(game.ply_count for game in source.games),
            object_keys=tuple(stored_keys),
        )
