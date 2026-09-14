"""B2a acquisition orchestration from a source reference to a revision."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import tempfile
from typing import Mapping
from urllib.parse import urlparse, urlunparse

import chess.pgn

from .canonicalize import CanonicalizationResult, canonicalize_source_file
from .registry import Registry
from .sources.base import SourceAdapter, SourceDescriptor, SourceRef
from .storage import ObjectStore
from .identity import identify_game


_STATE_RANK = {
    "DISCOVERED": 0,
    "DOWNLOADED": 1,
    "VALIDATED": 2,
    "CANONICALIZED": 3,
    "SHARDED": 4,
    "READY": 5,
}


@dataclass(frozen=True)
class AcquisitionResult:
    """Audit summary for one source acquisition and canonical revision."""

    source_ref: SourceRef
    tournament_id: int
    source_tournament_id: int
    source_file_id: int
    raw_object_key: str
    raw_sha256: str
    raw_byte_size: int
    revision_id: int
    revision_number: int
    canonical_sha256: str
    canonical_game_count: int
    canonical_ply_count: int
    canonical_path: Path
    source_id: int = 0
    download_attempt_id: int = 0
    tournament_status: str = "CANONICALIZED"
    source_game_count: int = 0
    valid_game_count: int = 0
    invalid_game_count: int = 0


@dataclass(frozen=True)
class _ValidationSummary:
    source_game_count: int
    valid_game_count: int
    invalid_game_count: int


def _stable_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    name = type(error).__name__
    return f"{name}: {message}" if message else name


def _source_base_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("source reference URL must be absolute")
    return urlunparse(
        parsed._replace(path="", params="", query="", fragment="")
    )


def _validation_summary(raw_bytes: bytes) -> _ValidationSummary:
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("raw PGN must be UTF-8, with an optional BOM") from exc

    stream = io.StringIO(raw_text)
    source_game_count = 0
    valid_game_count = 0
    invalid_game_count = 0
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        source_game_count += 1
        if game.errors:
            invalid_game_count += 1
            continue
        try:
            identify_game(game)
        except (KeyError, TypeError, ValueError):
            invalid_game_count += 1
            continue
        valid_game_count += 1

    if valid_game_count == 0:
        raise ValueError("downloaded PGN contains no legal games")
    return _ValidationSummary(
        source_game_count=source_game_count,
        valid_game_count=valid_game_count,
        invalid_game_count=invalid_game_count,
    )


def _download_status(adapter: SourceAdapter) -> int | None:
    value = getattr(adapter, "last_download_status", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _download_filename(adapter: SourceAdapter, downloaded_path: Path) -> str:
    value = getattr(adapter, "last_download_filename", None)
    if value:
        return str(value)
    return downloaded_path.name


def _download_content_type(adapter: SourceAdapter) -> str | None:
    value = getattr(adapter, "last_download_content_type", None)
    return None if value is None else str(value)


class AcquisitionService:
    """Orchestrate one explicit B2 source adapter through canonicalization."""

    def __init__(
        self,
        registry: Registry,
        store: ObjectStore,
        workspace: Path,
        adapters: Mapping[str, SourceAdapter],
    ) -> None:
        self.registry = registry
        self.store = store
        self.workspace = Path(workspace).expanduser()
        self.workspace.mkdir(parents=True, exist_ok=True)
        if not self.workspace.is_dir():
            raise NotADirectoryError(self.workspace)
        self.adapters = dict(adapters)

    def _resolve_adapter(
        self,
        query: str,
    ) -> tuple[str, SourceAdapter, SourceRef]:
        if not isinstance(query, str) or not query:
            raise ValueError("source query must be a non-empty string")

        matches: list[tuple[str, SourceAdapter, SourceRef]] = []
        for provider in sorted(self.adapters):
            adapter = self.adapters[provider]
            try:
                refs = adapter.discover(query)
            except ValueError:
                continue
            if refs is None:
                raise TypeError(
                    f"source adapter {provider!r} returned no discovery result"
                )
            for ref in refs:
                if not isinstance(ref, SourceRef):
                    raise TypeError(
                        f"source adapter {provider!r} returned an invalid source ref"
                    )
                if ref.provider != provider:
                    raise ValueError(
                        f"source adapter {provider!r} returned ref for "
                        f"provider {ref.provider!r}"
                    )
                matches.append((provider, adapter, ref))

        if not matches:
            raise ValueError(
                f"no registered source adapter can discover query {query!r}"
            )
        if len(matches) != 1:
            providers = ", ".join(provider for provider, _, _ in matches)
            raise ValueError(
                f"ambiguous source adapter match for query {query!r}: {providers}"
            )
        return matches[0]

    def _canonical_path(self, tournament_id: int, raw_sha256: str) -> Path:
        return (
            self.workspace
            / "canonical"
            / str(tournament_id)
            / f"{raw_sha256}.pgn"
        )

    def _advance_status(self, tournament_id: int, status: str) -> None:
        current = self.registry.get_tournament_status(tournament_id)
        if _STATE_RANK[status] > _STATE_RANK[current]:
            self.registry.set_tournament_status(tournament_id, status)

    def acquire(self, query: str) -> AcquisitionResult:
        """Acquire one source query and return its canonical tournament revision."""
        provider, adapter, source_ref = self._resolve_adapter(query)
        descriptor = adapter.describe(source_ref)
        if not isinstance(descriptor, SourceDescriptor):
            raise TypeError(
                f"source adapter {provider!r} returned an invalid descriptor"
            )

        source_id = self.registry.upsert_source(
            name=provider,
            base_url=_source_base_url(source_ref.source_url),
        )
        tournament_id = self.registry.upsert_tournament(
            slug=f"{provider}-{source_ref.external_id}",
            name=descriptor.title,
            site=_source_base_url(source_ref.source_url),
            country=descriptor.event_country_hint,
            time_control_class=descriptor.time_control_hint,
            is_otb=descriptor.is_otb_hint,
            status="DISCOVERED",
        )
        source_tournament_id = self.registry.upsert_source_tournament(
            source_id=source_id,
            tournament_id=tournament_id,
            external_id=source_ref.external_id,
            source_url=source_ref.source_url,
            pgn_url=descriptor.pgn_url,
        )
        download_attempt_id = self.registry.record_download_attempt(
            source_tournament_id=source_tournament_id,
        )

        download_root = self.workspace / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        source_file_id: int | None = None
        raw_object_key: str | None = None
        raw_sha256: str | None = None
        raw_byte_size = 0

        with tempfile.TemporaryDirectory(
            dir=download_root,
            prefix="acquisition-",
        ) as temporary_directory:
            downloaded_path = Path(temporary_directory) / "original.pgn"
            try:
                returned_path = adapter.download_pgn(source_ref, downloaded_path)
                downloaded_path = Path(returned_path).expanduser()
                if not downloaded_path.is_file():
                    raise RuntimeError("source adapter did not create a PGN file")
                raw_bytes = downloaded_path.read_bytes()
                raw_byte_size = len(raw_bytes)
                if raw_byte_size == 0:
                    raise ValueError("downloaded PGN is empty")
                raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
                raw_object_key = (
                    f"tournaments/{tournament_id}/source/{provider}/"
                    f"{raw_sha256}/original.pgn"
                )
                object_stat = self.store.put_file(raw_object_key, downloaded_path)
                if (
                    object_stat.sha256 != raw_sha256
                    or object_stat.size != raw_byte_size
                ):
                    raise RuntimeError(
                        "stored raw object failed checksum or size verification"
                    )
                source_file_id = self.registry.record_source_file(
                    source_tournament_id=source_tournament_id,
                    object_key=raw_object_key,
                    filename=_download_filename(adapter, downloaded_path),
                    sha256=raw_sha256,
                    byte_size=raw_byte_size,
                    content_type=_download_content_type(adapter),
                    source_url=source_ref.source_url,
                )
                self.registry.finish_download_attempt(
                    download_attempt_id,
                    source_file_id=source_file_id,
                    http_status=_download_status(adapter),
                )
            except Exception as exc:
                self.registry.finish_download_attempt(
                    download_attempt_id,
                    source_file_id=source_file_id,
                    http_status=_download_status(adapter),
                    error=_stable_error(exc),
                )
                raise

            self._advance_status(tournament_id, "DOWNLOADED")
            validation = _validation_summary(raw_bytes)
            self._advance_status(tournament_id, "VALIDATED")

            canonical_path = self._canonical_path(tournament_id, raw_sha256)
            canonical_result: CanonicalizationResult = canonicalize_source_file(
                self.registry,
                tournament_id,
                source_file_id,
                downloaded_path,
                canonical_path,
            )
            self._advance_status(tournament_id, "CANONICALIZED")
            final_status = self.registry.get_tournament_status(tournament_id)

        return AcquisitionResult(
            source_ref=source_ref,
            source_id=source_id,
            tournament_id=tournament_id,
            source_tournament_id=source_tournament_id,
            source_file_id=source_file_id,
            raw_object_key=raw_object_key,
            raw_sha256=raw_sha256,
            raw_byte_size=raw_byte_size,
            download_attempt_id=download_attempt_id,
            revision_id=canonical_result.revision_id,
            revision_number=canonical_result.revision_number,
            canonical_sha256=canonical_result.canonical_sha256,
            canonical_game_count=canonical_result.game_count,
            canonical_ply_count=canonical_result.ply_count,
            canonical_path=canonical_result.canonical_path,
            tournament_status=final_status,
            source_game_count=validation.source_game_count,
            valid_game_count=validation.valid_game_count,
            invalid_game_count=validation.invalid_game_count,
        )


__all__ = ["AcquisitionResult", "AcquisitionService"]
