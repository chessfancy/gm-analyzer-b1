"""SQLite-backed provenance registry for B2 acquisition data."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping

from .json_codec import canonical_json_bytes


REGISTRY_SCHEMA_VERSION = 1

STATES = (
    "DISCOVERED",
    "DOWNLOADED",
    "VALIDATED",
    "CANONICALIZED",
    "SHARDED",
    "READY",
)
_STATE_RANK = {state: index for index, state in enumerate(STATES)}
_UNSET = object()
_COUNTED_TABLES = (
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
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS registry_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    base_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT,
    site TEXT,
    country TEXT,
    start_date TEXT,
    end_date TEXT,
    time_control_class TEXT,
    is_otb INTEGER CHECK (is_otb IN (0, 1) OR is_otb IS NULL),
    has_vietnamese_player INTEGER CHECK (
        has_vietnamese_player IN (0, 1)
        OR has_vietnamese_player IS NULL
    ),
    priority_score INTEGER NOT NULL DEFAULT 0,
    priority_reasons_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS source_tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    source_url TEXT,
    pgn_url TEXT,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_id, external_id),
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_tournament_id INTEGER NOT NULL,
    tournament_id INTEGER,
    source_id INTEGER,
    source_url TEXT,
    object_key TEXT NOT NULL,
    filename TEXT,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    content_type TEXT,
    status TEXT NOT NULL DEFAULT 'DOWNLOADED',
    downloaded_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_tournament_id, sha256),
    FOREIGN KEY (source_tournament_id)
        REFERENCES source_tournaments(id) ON DELETE CASCADE,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE SET NULL,
    FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS download_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER,
    source_tournament_id INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    http_status INTEGER,
    error TEXT,
    FOREIGN KEY (source_file_id)
        REFERENCES source_files(id) ON DELETE SET NULL,
    FOREIGN KEY (source_tournament_id)
        REFERENCES source_tournaments(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS canonical_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint_version TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    variant TEXT NOT NULL,
    initial_fen TEXT NOT NULL,
    mainline_uci TEXT NOT NULL,
    ply_count INTEGER NOT NULL CHECK (ply_count >= 0),
    canonical_headers_json TEXT NOT NULL DEFAULT '{}',
    canonical_occurrence_id INTEGER,
    quality_score REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (fingerprint_version, fingerprint),
    FOREIGN KEY (canonical_occurrence_id)
        REFERENCES game_occurrences(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS game_occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_game_id INTEGER,
    tournament_id INTEGER NOT NULL,
    source_file_id INTEGER NOT NULL,
    source_game_index INTEGER NOT NULL CHECK (source_game_index >= 1),
    raw_headers_json TEXT NOT NULL DEFAULT '{}',
    raw_pgn_object_key TEXT,
    is_valid INTEGER NOT NULL DEFAULT 1 CHECK (is_valid IN (0, 1)),
    parse_error TEXT,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_file_id, source_game_index),
    FOREIGN KEY (canonical_game_id)
        REFERENCES canonical_games(id) ON DELETE SET NULL,
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE,
    FOREIGN KEY (source_file_id) REFERENCES source_files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS game_metadata_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_game_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    values_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (canonical_game_id, field),
    FOREIGN KEY (canonical_game_id)
        REFERENCES canonical_games(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tournament_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id INTEGER NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    canonical_sha256 TEXT NOT NULL,
    canonicalization_policy TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (tournament_id, revision_number),
    UNIQUE (
        tournament_id,
        canonical_sha256,
        canonicalization_policy
    ),
    FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tournament_games (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL,
    canonical_game_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    selected_occurrence_id INTEGER,
    UNIQUE (revision_id, ordinal),
    UNIQUE (revision_id, canonical_game_id),
    FOREIGN KEY (revision_id)
        REFERENCES tournament_revisions(id) ON DELETE CASCADE,
    FOREIGN KEY (canonical_game_id)
        REFERENCES canonical_games(id) ON DELETE CASCADE,
    FOREIGN KEY (selected_occurrence_id)
        REFERENCES game_occurrences(id) ON DELETE SET NULL
);
"""


def _timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _state(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("tournament status must be a string")
    normalized = value.strip().upper()
    if normalized not in _STATE_RANK:
        raise ValueError(f"unknown tournament status: {value!r}")
    return normalized


def _json_text(value: object, default: object) -> str:
    if value is None:
        value = default
    elif isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    return canonical_json_bytes(value).decode("utf-8")


def _bool_value(value: bool | int | None) -> int | None:
    if value is None:
        return None
    return int(bool(value))


def _row_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


class Registry:
    """Repository for the dedicated B2 ``registry.sqlite`` database."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self._read_only = False
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    @classmethod
    def open_read_only(cls, path: Path) -> "Registry":
        """Open an existing registry without schema initialization or writes."""
        registry_path = Path(path).expanduser()
        if registry_path.is_dir():
            raise ValueError("registry path must be a file, not a directory")
        if str(registry_path) == ":memory:" or not registry_path.is_file():
            raise FileNotFoundError(f"registry does not exist: {registry_path}")

        registry = cls.__new__(cls)
        registry.path = registry_path
        registry._read_only = True
        return registry

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            connection = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                timeout=30,
                uri=True,
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def ensure_schema(self) -> None:
        """Create or validate schema version 1 without destructive changes."""
        with self._connect() as connection:
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if user_version > REGISTRY_SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported registry schema version: {user_version}"
                )

            connection.executescript(_SCHEMA_SQL)
            row = connection.execute(
                "SELECT value FROM registry_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO registry_meta(key, value) VALUES ('schema_version', ?)",
                    (str(REGISTRY_SCHEMA_VERSION),),
                )
            elif row[0] != str(REGISTRY_SCHEMA_VERSION):
                raise RuntimeError(f"unsupported registry schema version: {row[0]}")

            connection.execute(f"PRAGMA user_version = {REGISTRY_SCHEMA_VERSION}")

    def upsert_source(
        self,
        name: str,
        base_url: str | None = None,
        *,
        enabled: bool = True,
        priority: int = 0,
    ) -> int:
        if not isinstance(name, str) or not name:
            raise ValueError("source name must not be empty")
        now = _timestamp()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sources(name, base_url, enabled, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    base_url = excluded.base_url,
                    enabled = excluded.enabled,
                    priority = excluded.priority,
                    updated_at = excluded.updated_at
                """,
                (name, base_url, int(bool(enabled)), int(priority), now, now),
            )
            row = connection.execute(
                "SELECT id FROM sources WHERE name = ?", (name,)
            ).fetchone()
            return int(row[0])

    def upsert_tournament(
        self,
        slug: str,
        name: str | None = None,
        site: str | None = None,
        country: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        time_control_class: str | None = None,
        is_otb: bool | int | None = None,
        has_vietnamese_player: bool | int | None = None,
        priority_score: int | object = _UNSET,
        priority_reasons: object | None = None,
        *,
        status: str = "DISCOVERED",
        priority_reasons_json: str | None = None,
    ) -> int:
        if not isinstance(slug, str) or not slug:
            raise ValueError("tournament slug must not be empty")
        status = _state(status)
        reasons_value = (
            priority_reasons_json
            if priority_reasons_json is not None
            else priority_reasons
        )
        reasons_json = _json_text(reasons_value, [])
        new_priority_score = (
            0
            if priority_score is _UNSET or priority_score is None
            else int(priority_score)
        )
        now = _timestamp()

        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM tournaments WHERE slug = ?", (slug,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO tournaments(
                        slug, name, site, country, start_date, end_date,
                        time_control_class, is_otb, has_vietnamese_player,
                        priority_score, priority_reasons_json, status,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        name,
                        site,
                        country,
                        start_date,
                        end_date,
                        time_control_class,
                        _bool_value(is_otb),
                        _bool_value(has_vietnamese_player),
                        new_priority_score,
                        reasons_json,
                        status,
                        now,
                        now,
                    ),
                )
                return int(connection.execute(
                    "SELECT id FROM tournaments WHERE slug = ?", (slug,)
                ).fetchone()[0])

            tournament_id = int(existing["id"])
            stored_priority_score = (
                existing["priority_score"]
                if priority_score is _UNSET or priority_score is None
                else int(priority_score)
            )
            stored_reasons_json = (
                existing["priority_reasons_json"]
                if reasons_value is None
                else reasons_json
            )
            connection.execute(
                """
                UPDATE tournaments
                SET name = ?, site = ?, country = ?, start_date = ?, end_date = ?,
                    time_control_class = ?, is_otb = ?,
                    has_vietnamese_player = ?, priority_score = ?,
                    priority_reasons_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    existing["name"] if name is None else name,
                    existing["site"] if site is None else site,
                    existing["country"] if country is None else country,
                    existing["start_date"] if start_date is None else start_date,
                    existing["end_date"] if end_date is None else end_date,
                    (
                        existing["time_control_class"]
                        if time_control_class is None
                        else time_control_class
                    ),
                    existing["is_otb"] if is_otb is None else _bool_value(is_otb),
                    (
                        existing["has_vietnamese_player"]
                        if has_vietnamese_player is None
                        else _bool_value(has_vietnamese_player)
                    ),
                    stored_priority_score,
                    stored_reasons_json,
                    now,
                    tournament_id,
                ),
            )
            current_status = _state(str(existing["status"]))
            if _STATE_RANK[status] > _STATE_RANK[current_status]:
                connection.execute(
                    "UPDATE tournaments SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, tournament_id),
                )
            return tournament_id

    def set_tournament_status(self, tournament_id: int, status: str) -> None:
        status = _state(status)
        now = _timestamp()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM tournaments WHERE id = ?", (tournament_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown tournament id: {tournament_id}")
            current = _state(str(row[0]))
            if _STATE_RANK[status] < _STATE_RANK[current]:
                raise ValueError(
                    f"tournament status cannot regress from {current} to {status}"
                )
            if status != current:
                connection.execute(
                    "UPDATE tournaments SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, tournament_id),
                )

    def upsert_source_tournament(
        self,
        source_id: int,
        tournament_id: int,
        external_id: str,
        source_url: str | None = None,
        pgn_url: str | None = None,
        *,
        discovered_at: str | None = None,
        last_seen_at: str | None = None,
    ) -> int:
        if not isinstance(external_id, str) or not external_id:
            raise ValueError("source tournament external_id must not be empty")
        discovered_at = discovered_at or _timestamp()
        last_seen_at = last_seen_at or _timestamp()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, tournament_id FROM source_tournaments
                WHERE source_id = ? AND external_id = ?
                """,
                (source_id, external_id),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO source_tournaments(
                        tournament_id, source_id, external_id, source_url, pgn_url,
                        discovered_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tournament_id,
                        source_id,
                        external_id,
                        source_url,
                        pgn_url,
                        discovered_at,
                        last_seen_at,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id FROM source_tournaments
                    WHERE source_id = ? AND external_id = ?
                    """,
                    (source_id, external_id),
                ).fetchone()
                return int(row[0])

            source_tournament_id = int(existing[0])
            if int(existing["tournament_id"]) != int(tournament_id):
                raise ValueError(
                    "source tournament identity cannot move between tournaments"
                )
            connection.execute(
                """
                UPDATE source_tournaments
                SET source_url = COALESCE(?, source_url),
                    pgn_url = COALESCE(?, pgn_url), last_seen_at = ?
                WHERE id = ?
                """,
                (
                    source_url,
                    pgn_url,
                    last_seen_at,
                    source_tournament_id,
                ),
            )
            return source_tournament_id

    def record_source_file(
        self,
        source_tournament_id: int,
        object_key: str,
        filename: str,
        sha256: str,
        byte_size: int,
        content_type: str | None = None,
        *,
        status: str = "DOWNLOADED",
        downloaded_at: str | None = None,
        source_url: str | None = None,
    ) -> int:
        if not object_key:
            raise ValueError("source file object_key must not be empty")
        if not sha256:
            raise ValueError("source file sha256 must not be empty")
        if int(byte_size) < 0:
            raise ValueError("source file byte_size must not be negative")
        downloaded_at = downloaded_at or _timestamp()
        with self._connect() as connection:
            provenance = connection.execute(
                """
                SELECT tournament_id, source_id, source_url
                FROM source_tournaments WHERE id = ?
                """,
                (source_tournament_id,),
            ).fetchone()
            if provenance is None:
                raise KeyError(f"unknown source tournament id: {source_tournament_id}")

            existing = connection.execute(
                """
                SELECT id, byte_size, sha256 FROM source_files
                WHERE source_tournament_id = ? AND sha256 = ?
                """,
                (source_tournament_id, sha256),
            ).fetchone()
            if existing is not None:
                if int(existing["byte_size"]) != int(byte_size):
                    raise ValueError(
                        "source file SHA256 is already registered with a different size"
                    )
                return int(existing["id"])

            connection.execute(
                """
                INSERT INTO source_files(
                    source_tournament_id, tournament_id, source_id, source_url,
                    object_key, filename, sha256, byte_size, content_type,
                    status, downloaded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_tournament_id,
                    provenance["tournament_id"],
                    provenance["source_id"],
                    source_url or provenance["source_url"],
                    object_key,
                    filename,
                    sha256,
                    int(byte_size),
                    content_type,
                    status,
                    downloaded_at,
                ),
            )
            row = connection.execute(
                "SELECT id FROM source_files WHERE source_tournament_id = ? AND sha256 = ?",
                (source_tournament_id, sha256),
            ).fetchone()
            return int(row[0])

    def get_source_file(self, source_file_id: int) -> dict[str, object]:
        """Return immutable source-file provenance as a plain dictionary."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_files WHERE id = ?", (source_file_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown source file id: {source_file_id}")
            return _row_dict(row)

    def record_download_attempt(
        self,
        source_file_id: int | None = None,
        *,
        source_tournament_id: int | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        http_status: int | None = None,
        error: str | None = None,
    ) -> int:
        started_at = started_at or _timestamp()
        with self._connect() as connection:
            if source_file_id is not None:
                row = connection.execute(
                    "SELECT source_tournament_id FROM source_files WHERE id = ?",
                    (source_file_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown source file id: {source_file_id}")
                if source_tournament_id is None:
                    source_tournament_id = row[0]
            elif source_tournament_id is not None:
                row = connection.execute(
                    "SELECT id FROM source_tournaments WHERE id = ?",
                    (source_tournament_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(
                        f"unknown source tournament id: {source_tournament_id}"
                    )

            cursor = connection.execute(
                """
                INSERT INTO download_attempts(
                    source_file_id, source_tournament_id, started_at,
                    finished_at, http_status, error
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_file_id,
                    source_tournament_id,
                    started_at,
                    finished_at,
                    http_status,
                    error,
                ),
            )
            return int(cursor.lastrowid)

    def finish_download_attempt(
        self,
        attempt_id: int,
        *,
        source_file_id: int | None = None,
        finished_at: str | None = None,
        http_status: int | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, source_tournament_id FROM download_attempts
                WHERE id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown download attempt id: {attempt_id}")
            if source_file_id is None:
                connection.execute(
                    """
                    UPDATE download_attempts
                    SET finished_at = ?, http_status = ?, error = ?
                    WHERE id = ?
                    """,
                    (finished_at or _timestamp(), http_status, error, attempt_id),
                )
                return

            source_file = connection.execute(
                """
                SELECT source_tournament_id FROM source_files WHERE id = ?
                """,
                (source_file_id,),
            ).fetchone()
            if source_file is None:
                raise KeyError(f"unknown source file id: {source_file_id}")
            if (
                row["source_tournament_id"] is not None
                and int(row["source_tournament_id"])
                != int(source_file["source_tournament_id"])
            ):
                raise ValueError(
                    "download attempt and source file provenance do not match"
                )
            connection.execute(
                """
                UPDATE download_attempts
                SET source_file_id = ?, finished_at = ?, http_status = ?, error = ?
                WHERE id = ?
                """,
                (
                    source_file_id,
                    finished_at or _timestamp(),
                    http_status,
                    error,
                    attempt_id,
                ),
            )

    def upsert_canonical_game(
        self,
        fingerprint_version: str | object,
        fingerprint: str | None = None,
        variant: str | None = None,
        initial_fen: str | None = None,
        mainline_uci: Iterable[str] | str | None = None,
        ply_count: int | None = None,
        canonical_headers: object | None = None,
        canonical_occurrence_id: int | None = None,
        quality_score: float | None = None,
        *,
        canonical_headers_json: str | None = None,
    ) -> int:
        if not isinstance(fingerprint_version, str) and fingerprint is None:
            identity = fingerprint_version
            fingerprint_version = str(identity.fingerprint_version)
            fingerprint = str(identity.fingerprint)
            variant = str(identity.variant)
            initial_fen = str(identity.initial_fen)
            mainline_uci = identity.mainline_uci
            ply_count = int(identity.ply_count)

        if not isinstance(fingerprint_version, str) or not fingerprint_version:
            raise ValueError("fingerprint_version must not be empty")
        if not fingerprint:
            raise ValueError("fingerprint must not be empty")
        if variant is None or initial_fen is None or mainline_uci is None:
            raise ValueError("canonical game identity fields are required")

        if isinstance(mainline_uci, str):
            try:
                parsed_moves = json.loads(mainline_uci)
            except json.JSONDecodeError:
                parsed_moves = [mainline_uci]
        else:
            parsed_moves = list(mainline_uci)
        moves = tuple(str(move) for move in parsed_moves)
        mainline_json = canonical_json_bytes(list(moves)).decode("utf-8")
        if ply_count is None:
            ply_count = len(moves)
        if int(ply_count) != len(moves):
            raise ValueError("ply_count must equal the mainline move count")
        headers_value = (
            canonical_headers_json
            if canonical_headers_json is not None
            else canonical_headers
        )
        headers_json = _json_text(headers_value, {})

        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, variant, initial_fen, mainline_uci, ply_count
                FROM canonical_games
                WHERE fingerprint_version = ? AND fingerprint = ?
                """,
                (fingerprint_version, fingerprint),
            ).fetchone()
            if existing is not None:
                if (
                    existing["variant"] != variant
                    or existing["initial_fen"] != initial_fen
                    or existing["mainline_uci"] != mainline_json
                    or int(existing["ply_count"]) != int(ply_count)
                ):
                    raise ValueError(
                        "canonical fingerprint is already bound to different chess content"
                    )
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO canonical_games(
                    fingerprint_version, fingerprint, variant, initial_fen,
                    mainline_uci, ply_count, canonical_headers_json,
                    canonical_occurrence_id, quality_score
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint_version,
                    fingerprint,
                    variant,
                    initial_fen,
                    mainline_json,
                    int(ply_count),
                    headers_json,
                    canonical_occurrence_id,
                    quality_score,
                ),
            )
            return int(cursor.lastrowid)

    def record_occurrence(
        self,
        canonical_game_id: int | None,
        tournament_id: int,
        source_file_id: int,
        source_game_index: int,
        raw_headers: object | None = None,
        raw_pgn_object_key: str | None = None,
        *,
        discovered_at: str | None = None,
        is_valid: bool = True,
        parse_error: str | None = None,
    ) -> int:
        if int(source_game_index) < 1:
            raise ValueError("source_game_index must be at least 1")
        headers_json = _json_text(raw_headers, {})
        discovered_at = discovered_at or _timestamp()
        with self._connect() as connection:
            source_file = connection.execute(
                "SELECT tournament_id FROM source_files WHERE id = ?",
                (source_file_id,),
            ).fetchone()
            if source_file is None:
                raise ValueError(f"unknown source file id: {source_file_id}")
            if (
                source_file["tournament_id"] is None
                or int(source_file["tournament_id"]) != int(tournament_id)
            ):
                raise ValueError(
                    "source file provenance does not match tournament"
                )

            existing = connection.execute(
                """
                SELECT id, canonical_game_id, tournament_id
                FROM game_occurrences
                WHERE source_file_id = ? AND source_game_index = ?
                """,
                (source_file_id, int(source_game_index)),
            ).fetchone()
            if existing is not None:
                if int(existing["tournament_id"]) != int(tournament_id):
                    raise ValueError(
                        "existing occurrence provenance does not match tournament"
                    )
                existing_game_id = existing["canonical_game_id"]
                if (
                    existing_game_id is not None
                    and canonical_game_id is not None
                    and int(existing_game_id) != int(canonical_game_id)
                ):
                    raise ValueError(
                        "source occurrence is already linked to another canonical game"
                    )
                return int(existing["id"])

            cursor = connection.execute(
                """
                INSERT INTO game_occurrences(
                    canonical_game_id, tournament_id, source_file_id,
                    source_game_index, raw_headers_json, raw_pgn_object_key,
                    is_valid, parse_error, discovered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_game_id,
                    tournament_id,
                    source_file_id,
                    int(source_game_index),
                    headers_json,
                    raw_pgn_object_key,
                    int(bool(is_valid)),
                    parse_error,
                    discovered_at,
                ),
            )
            return int(cursor.lastrowid)

    def list_valid_occurrence_candidates(
        self,
        canonical_game_id: int,
        *,
        tournament_id: int | None = None,
    ) -> list[dict[str, object]]:
        """List valid occurrences with source precedence metadata.

        The ordering is the canonical global/display precedence used by B2:
        source priority descending, then source name and file SHA ascending,
        then the source-local game index.
        """
        parameters: list[object] = [canonical_game_id]
        tournament_clause = ""
        if tournament_id is not None:
            tournament_clause = " AND go.tournament_id = ?"
            parameters.append(tournament_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT go.*, sf.sha256 AS source_file_sha256,
                       s.name AS source_name, s.priority AS source_priority
                FROM game_occurrences AS go
                JOIN source_files AS sf ON sf.id = go.source_file_id
                JOIN sources AS s ON s.id = sf.source_id
                WHERE go.canonical_game_id = ? AND go.is_valid = 1
                {tournament_clause}
                ORDER BY s.priority DESC, s.name ASC, sf.sha256 ASC,
                         go.source_game_index ASC, go.id ASC
                """,
                tuple(parameters),
            ).fetchall()
            return [_row_dict(row) for row in rows]

    def replace_metadata_conflicts(
        self,
        canonical_game_id: int,
        conflicts: Mapping[str, object],
    ) -> tuple[int, ...]:
        normalized: dict[str, str] = {}
        for field, values in conflicts.items():
            if not isinstance(field, str) or not field:
                raise ValueError("metadata conflict fields must be non-empty strings")
            if isinstance(values, (str, bytes)) or values is None:
                candidates = [values]
            else:
                candidates = list(values)  # type: ignore[arg-type]

            encoded_values = {
                canonical_json_bytes(candidate) for candidate in candidates
            }
            normalized[field] = (
                b"[" + b",".join(sorted(encoded_values)) + b"]"
            ).decode("utf-8")

        with self._connect() as connection:
            game = connection.execute(
                "SELECT id FROM canonical_games WHERE id = ?", (canonical_game_id,)
            ).fetchone()
            if game is None:
                raise KeyError(f"unknown canonical game id: {canonical_game_id}")

            existing_fields = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT field FROM game_metadata_conflicts
                    WHERE canonical_game_id = ?
                    """,
                    (canonical_game_id,),
                ).fetchall()
            }
            stale_fields = existing_fields - normalized.keys()
            for field in stale_fields:
                connection.execute(
                    """
                    DELETE FROM game_metadata_conflicts
                    WHERE canonical_game_id = ? AND field = ?
                    """,
                    (canonical_game_id, field),
                )

            now = _timestamp()
            for field, values_json in sorted(normalized.items()):
                connection.execute(
                    """
                    INSERT INTO game_metadata_conflicts(
                        canonical_game_id, field, values_json, updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(canonical_game_id, field) DO UPDATE SET
                        values_json = excluded.values_json,
                        updated_at = excluded.updated_at
                    """,
                    (canonical_game_id, field, values_json, now),
                )

            rows = connection.execute(
                """
                SELECT id FROM game_metadata_conflicts
                WHERE canonical_game_id = ? ORDER BY field
                """,
                (canonical_game_id,),
            ).fetchall()
            return tuple(int(row[0]) for row in rows)

    def create_or_get_revision(
        self,
        tournament_id: int,
        canonical_sha256: str,
        canonicalization_policy: str,
    ) -> tuple[int, int]:
        if not canonical_sha256 or not canonicalization_policy:
            raise ValueError("revision content hash and policy are required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, revision_number FROM tournament_revisions
                WHERE tournament_id = ? AND canonical_sha256 = ?
                  AND canonicalization_policy = ?
                """,
                (tournament_id, canonical_sha256, canonicalization_policy),
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), int(existing["revision_number"])

            next_number = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(revision_number), 0) + 1
                    FROM tournament_revisions WHERE tournament_id = ?
                    """,
                    (tournament_id,),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO tournament_revisions(
                    tournament_id, revision_number, canonical_sha256,
                    canonicalization_policy
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    tournament_id,
                    next_number,
                    canonical_sha256,
                    canonicalization_policy,
                ),
            )
            return int(cursor.lastrowid), next_number

    @staticmethod
    def _validate_selected_occurrence(
        connection: sqlite3.Connection,
        revision_tournament_id: int,
        canonical_game_id: int,
        occurrence_id: int,
    ) -> None:
        occurrence = connection.execute(
            """
            SELECT canonical_game_id, tournament_id, is_valid
            FROM game_occurrences WHERE id = ?
            """,
            (occurrence_id,),
        ).fetchone()
        if occurrence is None:
            raise ValueError("selected occurrence does not exist")
        if (
            occurrence["canonical_game_id"] is None
            or int(occurrence["canonical_game_id"]) != int(canonical_game_id)
        ):
            raise ValueError("selected occurrence does not belong to canonical game")
        if (
            occurrence["tournament_id"] is None
            or int(occurrence["tournament_id"]) != int(revision_tournament_id)
        ):
            raise ValueError("selected occurrence is not from revision tournament")
        if int(occurrence["is_valid"]) != 1:
            raise ValueError("selected occurrence must be valid")

    def set_revision_games(
        self,
        revision_id: int,
        canonical_game_ids: Iterable[int],
        selected_occurrence_ids: Mapping[int, int] | None = None,
    ) -> None:
        selected_occurrence_ids = selected_occurrence_ids or {}
        unique_game_ids: list[int] = []
        seen: set[int] = set()
        for value in canonical_game_ids:
            game_id = int(value)
            if game_id in seen:
                continue
            seen.add(game_id)
            unique_game_ids.append(game_id)

        with self._connect() as connection:
            revision = connection.execute(
                "SELECT id, tournament_id FROM tournament_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(f"unknown tournament revision id: {revision_id}")

            for game_id in unique_game_ids:
                game = connection.execute(
                    "SELECT id FROM canonical_games WHERE id = ?", (game_id,)
                ).fetchone()
                if game is None:
                    raise KeyError(f"unknown canonical game id: {game_id}")
                occurrence_id = selected_occurrence_ids.get(game_id)
                if occurrence_id is not None:
                    self._validate_selected_occurrence(
                        connection,
                        int(revision["tournament_id"]),
                        game_id,
                        int(occurrence_id),
                    )

            connection.execute(
                "DELETE FROM tournament_games WHERE revision_id = ?",
                (revision_id,),
            )
            for ordinal, game_id in enumerate(unique_game_ids, start=1):
                connection.execute(
                    """
                    INSERT INTO tournament_games(
                        revision_id, canonical_game_id, ordinal,
                        selected_occurrence_id
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        game_id,
                        ordinal,
                        selected_occurrence_ids.get(game_id),
                    ),
                )

    def add_revision_game(
        self,
        revision_id: int,
        canonical_game_id: int,
        ordinal: int | None = None,
        selected_occurrence_id: int | None = None,
    ) -> int:
        with self._connect() as connection:
            revision = connection.execute(
                "SELECT id, tournament_id FROM tournament_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(f"unknown tournament revision id: {revision_id}")
            game = connection.execute(
                "SELECT id FROM canonical_games WHERE id = ?", (canonical_game_id,)
            ).fetchone()
            if game is None:
                raise KeyError(f"unknown canonical game id: {canonical_game_id}")
            if selected_occurrence_id is not None:
                self._validate_selected_occurrence(
                    connection,
                    int(revision["tournament_id"]),
                    canonical_game_id,
                    int(selected_occurrence_id),
                )

            existing = connection.execute(
                """
                SELECT id FROM tournament_games
                WHERE revision_id = ? AND canonical_game_id = ?
                """,
                (revision_id, canonical_game_id),
            ).fetchone()
            if existing is not None:
                return int(existing[0])
            if ordinal is None:
                ordinal = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(ordinal), 0) + 1
                        FROM tournament_games WHERE revision_id = ?
                        """,
                        (revision_id,),
                    ).fetchone()[0]
                )
            if int(ordinal) < 1:
                raise ValueError("revision ordinal must be at least 1")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO tournament_games(
                        revision_id, canonical_game_id, ordinal,
                        selected_occurrence_id
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        canonical_game_id,
                        int(ordinal),
                        selected_occurrence_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    "revision ordinal is already occupied by another game"
                ) from exc
            return int(cursor.lastrowid)

    def get_tournament_status(self, tournament_id: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM tournaments WHERE id = ?", (tournament_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown tournament id: {tournament_id}")
            return _state(str(row[0]))

    def get_tournament(self, tournament_id: int) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tournaments WHERE id = ?", (tournament_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown tournament id: {tournament_id}")
            return _row_dict(row)

    def get_canonical_game(
        self,
        fingerprint: str,
        fingerprint_version: str = "game_fingerprint_v1",
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM canonical_games
                WHERE fingerprint_version = ? AND fingerprint = ?
                """,
                (fingerprint_version, fingerprint),
            ).fetchone()
            return None if row is None else _row_dict(row)

    def get_canonical_game_by_id(
        self, canonical_game_id: int
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM canonical_games WHERE id = ?",
                (canonical_game_id,),
            ).fetchone()
            return None if row is None else _row_dict(row)

    def set_canonical_display_occurrence(
        self,
        canonical_game_id: int,
        occurrence_id: int,
        canonical_headers: object | None = None,
    ) -> None:
        """Set the global display occurrence and copied source headers."""
        with self._connect() as connection:
            game = connection.execute(
                "SELECT id FROM canonical_games WHERE id = ?",
                (canonical_game_id,),
            ).fetchone()
            if game is None:
                raise KeyError(f"unknown canonical game id: {canonical_game_id}")
            occurrence = connection.execute(
                """
                SELECT canonical_game_id, is_valid, raw_headers_json
                FROM game_occurrences WHERE id = ?
                """,
                (occurrence_id,),
            ).fetchone()
            if (
                occurrence is None
                or occurrence["canonical_game_id"] is None
                or int(occurrence["canonical_game_id"]) != int(canonical_game_id)
                or int(occurrence["is_valid"]) != 1
            ):
                raise ValueError("canonical display occurrence must be a valid occurrence")
            headers_json = _json_text(
                occurrence["raw_headers_json"]
                if canonical_headers is None
                else canonical_headers,
                {},
            )
            connection.execute(
                """
                UPDATE canonical_games
                SET canonical_occurrence_id = ?, canonical_headers_json = ?
                WHERE id = ?
                """,
                (occurrence_id, headers_json, canonical_game_id),
            )

    def get_occurrences(self, canonical_game_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM game_occurrences
                WHERE canonical_game_id = ? ORDER BY id
                """,
                (canonical_game_id,),
            ).fetchall()
            return [_row_dict(row) for row in rows]

    def get_metadata_conflicts(self, canonical_game_id: int) -> dict[str, object]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT field, values_json FROM game_metadata_conflicts
                WHERE canonical_game_id = ? ORDER BY field
                """,
                (canonical_game_id,),
            ).fetchall()
            return {str(row["field"]): json.loads(row["values_json"]) for row in rows}

    def get_revision_games(self, revision_id: int) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tg.id, tg.revision_id, tg.canonical_game_id,
                       tg.ordinal, tg.selected_occurrence_id,
                       cg.fingerprint_version, cg.fingerprint,
                       cg.ply_count
                FROM tournament_games AS tg
                JOIN canonical_games AS cg ON cg.id = tg.canonical_game_id
                WHERE tg.revision_id = ?
                ORDER BY tg.ordinal
                """,
                (revision_id,),
            ).fetchall()
            return [_row_dict(row) for row in rows]

    def get_revision_membership(self, revision_id: int) -> list[dict[str, object]]:
        return self.get_revision_games(revision_id)

    def registry_counts(self) -> dict[str, int]:
        """Return row counts for every B2a registry table."""
        with self._connect() as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in _COUNTED_TABLES
            }

    def tournament_status_counts(self) -> dict[str, int]:
        """Return counts for every state, including zero-valued states."""
        counts = {state: 0 for state in STATES}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM tournaments GROUP BY status"
            ).fetchall()
        for status, count in rows:
            counts[_state(str(status))] = int(count)
        return counts

    def list_tournaments(self, limit: int | None = None) -> list[dict[str, object]]:
        """List tournaments newest-last for scheduler/audit summaries."""
        query = """
            SELECT id, slug, name, status, priority_score, updated_at
            FROM tournaments ORDER BY id
        """
        parameters: tuple[object, ...] = ()
        if limit is not None:
            if int(limit) < 1:
                raise ValueError("tournament list limit must be at least 1")
            query += " LIMIT ?"
            parameters = (int(limit),)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "id": int(row["id"]),
                "slug": row["slug"],
                "name": row["name"],
                "status": _state(str(row["status"])),
                "priority_score": int(row["priority_score"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def list_source_tournaments_for_tournament(
        self, tournament_id: int
    ) -> list[dict[str, object]]:
        """List provider-level source references for one tournament."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT st.id, st.external_id, st.source_url, st.pgn_url,
                       st.discovered_at, st.last_seen_at,
                       s.id AS source_id, s.name AS source_name
                FROM source_tournaments AS st
                JOIN sources AS s ON s.id = st.source_id
                WHERE st.tournament_id = ?
                ORDER BY s.name, st.external_id, st.id
                """,
                (tournament_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "source_id": int(row["source_id"]),
                "source_name": row["source_name"],
                "external_id": row["external_id"],
                "source_url": row["source_url"],
                "pgn_url": row["pgn_url"],
                "discovered_at": row["discovered_at"],
                "last_seen_at": row["last_seen_at"],
            }
            for row in rows
        ]

    def list_source_files_for_tournament(
        self, tournament_id: int
    ) -> list[dict[str, object]]:
        """List immutable raw source files for one tournament."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source_tournament_id, object_key, filename, sha256,
                       byte_size, content_type, status, downloaded_at
                FROM source_files
                WHERE tournament_id = ?
                ORDER BY id
                """,
                (tournament_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "source_tournament_id": (
                    None
                    if row["source_tournament_id"] is None
                    else int(row["source_tournament_id"])
                ),
                "object_key": row["object_key"],
                "filename": row["filename"],
                "sha256": row["sha256"],
                "byte_size": int(row["byte_size"]),
                "content_type": row["content_type"],
                "status": row["status"],
                "downloaded_at": row["downloaded_at"],
            }
            for row in rows
        ]

    def list_revisions_for_tournament(
        self, tournament_id: int
    ) -> list[dict[str, object]]:
        """List canonical revisions with membership counts."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT tr.id, tr.revision_number, tr.canonical_sha256,
                       tr.canonicalization_policy, tr.created_at,
                       COUNT(tg.id) AS game_count
                FROM tournament_revisions AS tr
                LEFT JOIN tournament_games AS tg ON tg.revision_id = tr.id
                WHERE tr.tournament_id = ?
                GROUP BY tr.id
                ORDER BY tr.revision_number, tr.id
                """,
                (tournament_id,),
            ).fetchall()
        return [
            {
                "revision_id": int(row["id"]),
                "revision_number": int(row["revision_number"]),
                "canonical_sha256": row["canonical_sha256"],
                "canonicalization_policy": row["canonicalization_policy"],
                "game_count": int(row["game_count"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_tournament_summary(self, tournament_id: int) -> dict[str, object]:
        """Return one JSON-serializable tournament audit summary."""
        try:
            row = self.get_tournament(tournament_id)
        except KeyError as exc:
            raise KeyError(f"unknown tournament id: {tournament_id}") from exc

        try:
            reasons = json.loads(str(row["priority_reasons_json"]))
        except json.JSONDecodeError as exc:
            raise ValueError("stored tournament priority reasons are invalid") from exc
        if not isinstance(reasons, list):
            raise ValueError("stored tournament priority reasons must be a JSON array")

        def optional_int(value: object) -> int | None:
            return None if value is None else int(value)  # type: ignore[arg-type]

        return {
            "tournament": {
                "id": int(row["id"]),
                "slug": row["slug"],
                "name": row["name"],
                "site": row["site"],
                "country": row["country"],
                "start_date": row["start_date"],
                "end_date": row["end_date"],
                "time_control_class": row["time_control_class"],
                "is_otb": optional_int(row["is_otb"]),
                "has_vietnamese_player": optional_int(row["has_vietnamese_player"]),
                "priority_score": int(row["priority_score"]),
                "priority_reasons": reasons,
                "status": _state(str(row["status"])),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
            "source_tournaments": self.list_source_tournaments_for_tournament(
                tournament_id
            ),
            "source_files": self.list_source_files_for_tournament(tournament_id),
            "revisions": self.list_revisions_for_tournament(tournament_id),
        }
