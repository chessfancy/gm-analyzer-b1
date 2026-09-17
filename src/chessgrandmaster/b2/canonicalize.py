"""Canonical PGN projection and exact game deduplication for B2."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import time
from typing import Callable, Iterable, Mapping

import chess
import chess.pgn

from .identity import GameIdentity, identify_game
from .registry import Registry


CANONICALIZATION_POLICY = "canonical_pgn_v1"
CONFLICT_FIELDS = (
    "Event",
    "Site",
    "Date",
    "Round",
    "White",
    "Black",
    "Result",
)
_STANDARD_HEADER_ORDER = (
    "Event",
    "Site",
    "Date",
    "Round",
    "White",
    "Black",
    "Result",
)
_STANDARD_INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class CanonicalizationResult:
    """Audit summary for one canonical tournament revision."""

    tournament_id: int
    revision_id: int
    revision_number: int
    canonical_sha256: str
    game_count: int
    ply_count: int
    canonical_path: Path
    source_game_count: int = 0
    valid_game_count: int = 0
    invalid_game_count: int = 0
    duplicate_occurrence_count: int = 0
    metadata_conflict_count: int = 0


@dataclass(frozen=True)
class ProcessedGameResult:
    """One source game after parsing and identity validation."""

    source_game_index: int
    raw_headers: dict[str, str]
    identity: GameIdentity | None
    is_valid: bool
    parse_error: str | None = None
    parse_ms: float = 0.0
    identity_ms: float = 0.0
    parse_count: int = 0
    queue_wait_ms: float = 0.0


def _stable_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    name = type(error).__name__
    return f"{name}: {message}" if message else name


def _game_headers(game: chess.pgn.Game) -> dict[str, str]:
    return {str(key): str(value) for key, value in game.headers.items()}


def _occurrence_headers(row: Mapping[str, object]) -> dict[str, str]:
    try:
        value = json.loads(str(row["raw_headers_json"]))
    except json.JSONDecodeError as exc:
        raise ValueError("stored occurrence headers are not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("stored occurrence headers must be a JSON object")
    return {str(key): str(item) for key, item in value.items()}


def _make_initial_board(identity: GameIdentity) -> chess.Board:
    variant = identity.variant.casefold()
    if variant in {"standard", "chess", "normal"}:
        board: chess.Board = chess.Board()
    elif variant in {"chess960", "fischerandom"}:
        board = chess.Board(chess960=True)
    else:
        try:
            board_type = chess.variant.find_variant(variant)
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"cannot safely reconstruct unsupported variant: {identity.variant!r}"
            ) from exc
        board = board_type()

    full_fen = f"{identity.initial_fen} 0 1"
    try:
        board.set_fen(full_fen)
    except ValueError as exc:
        raise ValueError("canonical initial FEN cannot be reconstructed") from exc
    return board


def _replay_identity(identity: GameIdentity) -> chess.Board:
    board = _make_initial_board(identity)
    for ply, uci in enumerate(identity.mainline_uci, start=1):
        try:
            move = board.parse_uci(uci)
        except ValueError as exc:
            raise ValueError(
                f"canonical move {uci!r} is not legal at ply {ply}"
            ) from exc
        board.push(move)
    return board


def _ordered_headers(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    values = {str(key): str(value) for key, value in headers.items()}
    values.setdefault("Result", "*")
    ordered_keys = [key for key in _STANDARD_HEADER_ORDER if key in values]
    ordered_keys.extend(sorted(key for key in values if key not in ordered_keys))
    return [(key, values[key]) for key in ordered_keys]


def _projection_game(
    identity: GameIdentity,
    selected_headers: Mapping[str, str],
) -> chess.pgn.Game:
    _replay_identity(identity)
    headers = {str(key): str(value) for key, value in selected_headers.items()}
    if identity.initial_fen != _STANDARD_INITIAL_FEN:
        headers["SetUp"] = "1"
        stored_fen = headers.get("FEN", "")
        if " ".join(stored_fen.split()[:4]) != identity.initial_fen:
            headers["FEN"] = f"{identity.initial_fen} 0 1"

    game = chess.pgn.Game()
    game.headers.clear()
    for key, value in _ordered_headers(headers):
        game.headers[key] = value

    node = game
    board = _make_initial_board(identity)
    for ply, uci in enumerate(identity.mainline_uci, start=1):
        try:
            move = board.parse_uci(uci)
        except ValueError as exc:
            raise ValueError(
                f"canonical move {uci!r} is not legal at ply {ply}"
            ) from exc
        node = node.add_main_variation(move)
        board.push(move)
    return game


def _export_projection(identity: GameIdentity, headers: Mapping[str, str]) -> str:
    game = _projection_game(identity, headers)
    text = game.accept(
        chess.pgn.StringExporter(
            headers=True,
            variations=False,
            comments=False,
            columns=None,
        )
    )
    parsed = chess.pgn.read_game(io.StringIO(text))
    if parsed is None or parsed.errors:
        raise ValueError("canonical PGN projection did not parse as legal PGN")
    if identify_game(parsed) != identity:
        raise ValueError("canonical PGN projection changed chess content")
    return text


def _refresh_conflicts(registry: Registry, canonical_game_id: int) -> int:
    values: dict[str, set[str]] = {field: set() for field in CONFLICT_FIELDS}
    for occurrence in registry.get_occurrences(canonical_game_id):
        if int(occurrence["is_valid"]) != 1:
            continue
        headers = _occurrence_headers(occurrence)
        for field in CONFLICT_FIELDS:
            if field in headers:
                values[field].add(headers[field])

    conflicts = {
        field: sorted(field_values, key=lambda value: value.encode("utf-8"))
        for field, field_values in values.items()
        if len(field_values) > 1
    }
    conflict_ids = registry.replace_metadata_conflicts(
        canonical_game_id,
        conflicts,
    )
    return len(conflict_ids)


def _validate_source_file(
    registry: Registry,
    tournament_id: int,
    source_file_id: int,
    raw_path: Path,
) -> tuple[dict[str, object], bytes]:
    try:
        source_file = registry.get_source_file(source_file_id)
    except KeyError as exc:
        raise ValueError(f"source file {source_file_id} does not exist") from exc

    registered_tournament = source_file.get("tournament_id")
    if (
        registered_tournament is None
        or int(registered_tournament) != int(tournament_id)
    ):
        raise ValueError("source file provenance does not match tournament")

    try:
        path_stat = raw_path.stat()
    except OSError as exc:
        raise ValueError("raw PGN path must be a regular file") from exc
    if not raw_path.is_file() or not stat.S_ISREG(path_stat.st_mode):
        raise ValueError("raw PGN path must be a regular file")

    raw_bytes = raw_path.read_bytes()
    if len(raw_bytes) != int(source_file["byte_size"]):
        raise ValueError("raw PGN byte size does not match registered source file")
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256 != str(source_file["sha256"]):
        raise ValueError("raw PGN SHA256 does not match registered source file")
    return source_file, raw_bytes


def _record_invalid(
    registry: Registry,
    tournament_id: int,
    source_file_id: int,
    source_game_index: int,
    headers: Mapping[str, str],
    object_key: str,
    error: BaseException | str,
) -> None:
    parse_error = error if isinstance(error, str) else _stable_error(error)
    registry.record_occurrence(
        canonical_game_id=None,
        tournament_id=tournament_id,
        source_file_id=source_file_id,
        source_game_index=source_game_index,
        raw_headers=dict(headers),
        raw_pgn_object_key=object_key,
        is_valid=False,
        parse_error=parse_error,
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(f"{path}.part")
    try:
        with temporary.open("wb") as stream:
            for offset in range(0, len(content), _CHUNK_SIZE):
                stream.write(content[offset : offset + _CHUNK_SIZE])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def finalize_processed_games(
    registry: Registry,
    tournament_id: int,
    source_file_id: int,
    object_key: str,
    processed_games: Iterable[ProcessedGameResult],
    output_path: Path,
    canonicalization_policy: str = CANONICALIZATION_POLICY,
    *,
    timing_callback: Callable[[str, float], None] | None = None,
) -> CanonicalizationResult:
    """Write already-processed games into one canonical tournament revision."""
    output_path = Path(output_path).expanduser()
    processed = sorted(
        list(processed_games),
        key=lambda result: int(result.source_game_index),
    )
    indexes = [int(result.source_game_index) for result in processed]
    if any(index < 1 for index in indexes) or len(indexes) != len(set(indexes)):
        raise ValueError("processed source game indexes must be unique and positive")

    def emit_timing(stage: str, started: float) -> None:
        if timing_callback is not None:
            timing_callback(stage, (time.perf_counter() - started) * 1000.0)

    ordered_game_ids: list[int] = []
    seen_game_ids: set[int] = set()
    seen_fingerprints: set[str] = set()
    identities: dict[int, GameIdentity] = {}
    valid_game_count = 0
    invalid_game_count = 0
    duplicate_occurrence_count = 0

    sqlite_started = time.perf_counter()
    for result in processed:
        source_game_index = int(result.source_game_index)
        headers = {str(key): str(value) for key, value in result.raw_headers.items()}
        if not result.is_valid or result.identity is None:
            _record_invalid(
                registry,
                tournament_id,
                source_file_id,
                source_game_index,
                headers,
                object_key,
                result.parse_error or "ProcessingError: game was invalid",
            )
            invalid_game_count += 1
            continue

        identity = result.identity
        canonical_game_id = registry.upsert_canonical_game(identity)
        registry.record_occurrence(
            canonical_game_id=canonical_game_id,
            tournament_id=tournament_id,
            source_file_id=source_file_id,
            source_game_index=source_game_index,
            raw_headers=headers,
            raw_pgn_object_key=object_key,
            replace_invalid=True,
        )
        valid_game_count += 1
        if identity.fingerprint in seen_fingerprints:
            duplicate_occurrence_count += 1
        seen_fingerprints.add(identity.fingerprint)
        if canonical_game_id not in seen_game_ids:
            seen_game_ids.add(canonical_game_id)
            ordered_game_ids.append(canonical_game_id)
            identities[canonical_game_id] = identity
    emit_timing("sqlite_write", sqlite_started)

    if not ordered_game_ids:
        raise RuntimeError("canonicalization produced zero valid games")

    metadata_started = time.perf_counter()
    metadata_conflict_count = 0
    selected_occurrence_ids: dict[int, int] = {}
    selected_headers: dict[int, dict[str, str]] = {}
    for canonical_game_id in ordered_game_ids:
        metadata_conflict_count += _refresh_conflicts(
            registry,
            canonical_game_id,
        )
        global_candidates = registry.list_valid_occurrence_candidates(
            canonical_game_id
        )
        if not global_candidates:
            raise RuntimeError("canonical game has no valid source occurrence")
        global_occurrence = global_candidates[0]
        registry.set_canonical_display_occurrence(
            canonical_game_id,
            int(global_occurrence["id"]),
            _occurrence_headers(global_occurrence),
        )

        local_candidates = registry.list_valid_occurrence_candidates(
            canonical_game_id,
            tournament_id=tournament_id,
        )
        if not local_candidates:
            raise RuntimeError("canonical game has no local valid occurrence")
        local_occurrence = local_candidates[0]
        selected_occurrence_ids[canonical_game_id] = int(local_occurrence["id"])
        selected_headers[canonical_game_id] = _occurrence_headers(local_occurrence)
    emit_timing("metadata_display_selection", metadata_started)

    projection_started = time.perf_counter()
    projected_games = [
        _export_projection(identities[canonical_game_id], selected_headers[canonical_game_id])
        for canonical_game_id in ordered_game_ids
    ]
    canonical_bytes = ("\n\n".join(projected_games) + "\n").encode("utf-8")
    canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    emit_timing("projection_export", projection_started)

    _atomic_write(output_path, canonical_bytes)
    revision_started = time.perf_counter()
    revision_id, revision_number = registry.create_or_get_revision(
        tournament_id,
        canonical_sha256,
        canonicalization_policy,
    )
    registry.set_revision_games(
        revision_id,
        ordered_game_ids,
        selected_occurrence_ids,
    )
    emit_timing("revision_finalization", revision_started)

    return CanonicalizationResult(
        tournament_id=int(tournament_id),
        revision_id=revision_id,
        revision_number=revision_number,
        canonical_sha256=canonical_sha256,
        game_count=len(ordered_game_ids),
        ply_count=sum(identities[game_id].ply_count for game_id in ordered_game_ids),
        canonical_path=output_path,
        source_game_count=len(processed),
        valid_game_count=valid_game_count,
        invalid_game_count=invalid_game_count,
        duplicate_occurrence_count=duplicate_occurrence_count,
        metadata_conflict_count=metadata_conflict_count,
    )


def canonicalize_source_file(
    registry: Registry,
    tournament_id: int,
    source_file_id: int,
    raw_path: Path,
    output_path: Path,
    canonicalization_policy: str = CANONICALIZATION_POLICY,
) -> CanonicalizationResult:
    """Canonicalize one immutable raw PGN into a content-addressed revision."""
    raw_path = Path(raw_path).expanduser()
    output_path = Path(output_path).expanduser()
    source_file, raw_bytes = _validate_source_file(
        registry,
        tournament_id,
        source_file_id,
        raw_path,
    )
    if raw_path.resolve() == output_path.resolve():
        raise ValueError("canonical output path must differ from immutable raw path")
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("raw PGN must be UTF-8, with an optional BOM") from exc

    stream = io.StringIO(raw_text)
    object_key = str(source_file["object_key"])
    processed_games: list[ProcessedGameResult] = []

    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        source_game_index = len(processed_games) + 1
        headers = _game_headers(game)
        if game.errors:
            processed_games.append(
                ProcessedGameResult(
                    source_game_index=source_game_index,
                    raw_headers=headers,
                    identity=None,
                    is_valid=False,
                    parse_error="; ".join(
                        _stable_error(error) for error in game.errors
                    ),
                    parse_count=1,
                )
            )
            continue

        try:
            identity = identify_game(game)
            _replay_identity(identity)
        except (ValueError, KeyError, TypeError) as exc:
            processed_games.append(
                ProcessedGameResult(
                    source_game_index=source_game_index,
                    raw_headers=headers,
                    identity=None,
                    is_valid=False,
                    parse_error=_stable_error(exc),
                    parse_count=1,
                )
            )
            continue

        processed_games.append(
            ProcessedGameResult(
                source_game_index=source_game_index,
                raw_headers=headers,
                identity=identity,
                is_valid=True,
                parse_count=1,
            )
        )

    return finalize_processed_games(
        registry,
        tournament_id,
        source_file_id,
        object_key,
        processed_games,
        output_path,
        canonicalization_policy,
    )
