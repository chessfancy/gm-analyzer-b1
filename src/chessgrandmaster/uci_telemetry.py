"""JSON-safe serialization and compressed UCI event archives."""

from __future__ import annotations

import gzip
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.engine


SCHEMA_VERSION = "cgm-uci-events-1"


class UciArchiveError(RuntimeError):
    """Raised when parsed UCI telemetry cannot be archived."""


def _type_name(value):
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _color_name(color):
    return "white" if color is chess.WHITE else "black"


def _score_json(value):
    if isinstance(value, chess.engine.Cp):
        return {
            "__type__": "Cp",
            "cp": value.cp,
        }

    if isinstance(value, chess.engine.Mate):
        return {
            "__type__": "Mate",
            "moves": value.moves,
        }

    if isinstance(value, chess.engine.Score):
        result = {
            "__type__": type(value).__name__,
        }
        if value.is_mate():
            result["mate"] = value.mate()
        else:
            result["cp"] = value.score()
        return result

    return None


def _wdl_json(value):
    if isinstance(value, chess.engine.Wdl):
        return {
            "__type__": "Wdl",
            "wins": value.wins,
            "draws": value.draws,
            "losses": value.losses,
        }

    return None


def _stable_unknown_value(value):
    text = str(value)
    default_prefix = f"<{type(value).__name__} object at 0x"
    if text.startswith(default_prefix) and text.endswith(">"):
        return f"<{_type_name(value)}>"
    return re.sub(r"0x[0-9a-fA-F]+", "0x<address>", text)


def to_json_safe(value):
    """Convert a python-chess engine value into deterministic JSON data."""
    if value is None or isinstance(value, (bool, int, str)):
        return value

    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {
            "__type__": "float",
            "value": str(value),
        }

    if isinstance(value, chess.Move):
        return value.uci()

    if isinstance(value, chess.engine.PovScore):
        return {
            "__type__": "PovScore",
            "relative": to_json_safe(value.relative),
            "turn": _color_name(value.turn),
        }

    score = _score_json(value)
    if score is not None:
        return score

    if isinstance(value, chess.engine.PovWdl):
        return {
            "__type__": "PovWdl",
            "relative": to_json_safe(value.relative),
            "turn": _color_name(value.turn),
        }

    wdl = _wdl_json(value)
    if wdl is not None:
        return wdl

    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str):
                json_key = key
            else:
                json_key = json.dumps(
                    to_json_safe(key),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            result[json_key] = to_json_safe(item)
        return result

    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]

    return {
        "__type__": _type_name(value),
        "value": _stable_unknown_value(value),
    }


def info_to_json(info):
    """Serialize parsed UCI info to canonical JSON text."""
    return json.dumps(
        to_json_safe(info),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


# Descriptive aliases for callers that prefer the domain terminology.
serialize_info = to_json_safe
serialize_info_json = info_to_json


class UciEventArchive:
    """Write parsed engine events as one compressed JSONL stream."""

    def __init__(self, path, archive_root=None):
        self.path = Path(path)
        self.archive_root = (
            Path(archive_root).resolve()
            if archive_root is not None
            else None
        )
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.closed_at = None
        self.event_seq = 0
        self.event_count = 0
        self.record_count = 0
        self._closed = False

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = gzip.open(
                self.path,
                mode="wt",
                encoding="utf-8",
                newline="\n",
            )
        except (OSError, EOFError) as exc:
            raise UciArchiveError(
                f"Cannot create UCI archive {self.path}: {exc}"
            ) from exc

    def write(self, event, context, *, info=None, multipv=None):
        if self._closed:
            raise UciArchiveError("Cannot write to a closed UCI archive")

        record = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": context.get("run_id"),
            "execution_id": context.get("execution_id"),
            "worker_id": context.get("worker_id"),
            "engine_session_id": context.get("engine_session_id"),
            "game_id": context.get("game_id"),
            "source_game_index": context.get("source_game_index"),
            "move_id": context.get("move_id"),
            "ply": context.get("ply"),
            "source_search": context.get("source_search"),
            "event_seq": self.event_seq,
            "event": event,
            "multipv": multipv,
        }
        if event == "info":
            record["info"] = to_json_safe(info)

        try:
            self._stream.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
        except (OSError, TypeError, ValueError) as exc:
            raise UciArchiveError(
                f"Cannot write UCI archive {self.path}: {exc}"
            ) from exc

        self.event_seq += 1
        self.record_count += 1
        if event == "info":
            self.event_count += 1

    def write_info(self, context, info):
        self.write(
            "info",
            context,
            info=info,
            multipv=info.get("multipv", 1),
        )

    def close(self):
        if self._closed:
            return self.manifest()

        try:
            self._stream.flush()
            self._stream.close()
        except (OSError, EOFError) as exc:
            self._closed = True
            raise UciArchiveError(
                f"Cannot close UCI archive {self.path}: {exc}"
            ) from exc

        self._closed = True
        self.closed_at = datetime.now(timezone.utc).isoformat()
        return self.manifest()

    def manifest(self):
        if self.archive_root is not None:
            try:
                relative_path = self.path.resolve().relative_to(
                    self.archive_root
                ).as_posix()
            except ValueError as exc:
                raise UciArchiveError(
                    f"Archive path is outside archive root: {self.path}"
                ) from exc
        else:
            relative_path = self.path.as_posix()

        return {
            "relative_path": relative_path,
            "compression": "gzip",
            "event_count": self.event_count,
            "record_count": self.record_count,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
        }
