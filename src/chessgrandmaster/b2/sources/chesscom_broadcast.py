"""Chess.com/Chess24 structured OTB broadcast source adapter."""

from __future__ import annotations

from html.parser import HTMLParser
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

from ._common import (
    ResponseSnapshot,
    approved_url,
    ensure_pgn_signature,
    fetch_body,
    looks_like_html,
    make_opener,
    object_text,
    origin_base,
    parse_json_body,
    response_header,
    same_origin,
    stream_download,
)
from .base import SourceDescriptor, SourceRef


PROVIDER = "chesscom-broadcast"
USER_AGENT = "ChessGrandmaster acquisition/1.0"
_DEFAULT_BASE_URL = "https://www.chess.com"
_DEFAULT_HOSTS = {
    "chess.com",
    "www.chess.com",
    "chess24.com",
    "www.chess24.com",
}
_ID_PART_RE = re.compile(r"^[^/\\?#\s]+$")
_EXTERNAL_RE = re.compile(r"^event:(?P<event>[^/]+)/game:(?P<game>[^/]+)$")


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._capture = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"title", "h1", "h2"}:
            self._capture = True

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"title", "h1", "h2"}:
            self._capture = False

    def handle_data(self, data: str) -> None:
        if self._capture:
            self.parts.append(data)

    @property
    def title(self) -> str | None:
        value = " ".join(" ".join(self.parts).split())
        return value or None


def _part(value: object) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and _ID_PART_RE.fullmatch(value) else None


def _first(mapping: dict[str, Any], *names: str) -> object | None:
    lowered = {str(key).casefold(): value for key, value in mapping.items()}
    for name in names:
        if name.casefold() in lowered:
            return lowered[name.casefold()]
    return None


def _nested_id(value: object) -> object:
    if isinstance(value, dict):
        for name in ("id", "key", "slug", "event_id", "game_id"):
            if name in value:
                return value[name]
    return value


def _bool_value(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().casefold()
        if value in {"true", "yes", "1", "otb", "over-the-board"}:
            return True
        if value in {"false", "no", "0", "online", "internet"}:
            return False
    return None


def _mapping_identity(mapping: dict[str, Any], event_context: str | None = None):
    event = _part(_nested_id(_first(mapping, "event_id", "eventId", "event", "broadcast_id", "broadcastId")))
    game = _part(_nested_id(_first(mapping, "game_id", "gameId", "game", "broadcast_game_id", "broadcastGameId")))
    event = event or event_context
    if event is None or game is None:
        return None
    return event, game


def _is_online(mapping: dict[str, Any], text: str) -> bool:
    for name in ("online", "is_online", "internet"):
        if _bool_value(_first(mapping, name)) is True:
            return True
    return bool(re.search(r"\b(?:online|internet|player\s+archive)\b", text.casefold()))


def _is_otb(mapping: dict[str, Any], context_text: str = "") -> bool:
    if _is_online(mapping, context_text):
        return False
    for name in ("otb", "is_otb", "over_the_board", "overTheBoard"):
        value = _bool_value(_first(mapping, name))
        if value is not None:
            return value
    broadcast_value = _bool_value(_first(mapping, "broadcast", "is_broadcast", "structured_broadcast"))
    if broadcast_value is not None:
        return broadcast_value
    text = " ".join(
        str(_first(mapping, name) or "")
        for name in ("title", "name", "format", "mode", "type", "event_type")
    ).casefold()
    if re.search(r"\bover[ -]the[ -]board\b|\botb\b", text):
        return True
    return "broadcast" in context_text.casefold()


def _mapping_text(mapping: dict[str, Any]) -> str:
    return " ".join(str(value) for value in mapping.values() if isinstance(value, (str, int, float)))


def _iter_games(value: object, *, event_context: str | None = None, context_text: str = ""):
    if isinstance(value, list):
        for item in value:
            yield from _iter_games(item, event_context=event_context, context_text=context_text)
        return
    if not isinstance(value, dict):
        return
    identity = _mapping_identity(value, event_context)
    text = f"{context_text} {_mapping_text(value)}"
    if identity is not None and _is_otb(value, text):
        yield identity, value
    next_event = _part(_nested_id(_first(value, "event_id", "eventId", "event", "broadcast_id", "broadcastId"))) or event_context
    for key, child in value.items():
        if isinstance(child, (dict, list)):
            yield from _iter_games(child, event_context=next_event, context_text=f"{text} {key}")


def _identity_from_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query, keep_blank_values=True) if parsed.query else {}
    event = next(
        (query[name][0] for name in ("event", "event_id", "eventId", "broadcast") if query.get(name)),
        None,
    )
    game = next(
        (query[name][0] for name in ("game", "game_id", "gameId") if query.get(name)),
        None,
    )
    lower_parts = [part.casefold() for part in parts]
    for marker in ("broadcast", "event", "events"):
        if marker in lower_parts:
            index = lower_parts.index(marker)
            remaining = parts[index + 1 :]
            if remaining:
                event = event or remaining[0]
            for game_marker in ("game", "games"):
                if game_marker in [part.casefold() for part in remaining]:
                    game_index = [part.casefold() for part in remaining].index(game_marker)
                    if game_index + 1 < len(remaining):
                        game = game or remaining[game_index + 1]
            if game is None and len(remaining) >= 2:
                game = remaining[-1]
            break
    if game:
        game = str(game).removesuffix(".pgn")
    event_part, game_part = _part(event), _part(game)
    if event_part and game_part:
        return event_part, game_part
    return None


def _parse_external_id(value: str) -> tuple[str, str]:
    match = _EXTERNAL_RE.fullmatch(value)
    if match is None:
        raise ValueError("Chess.com/Chess24 external_id must identify event and game")
    return match.group("event"), match.group("game")


class ChessComBroadcastAdapter:
    """Acquire only structured Chess.com/Chess24 OTB broadcast games."""

    def __init__(
        self,
        timeout_sec: float = 30.0,
        opener: Any = None,
        *,
        session: Any = None,
        base_url: str = _DEFAULT_BASE_URL,
        allowed_hosts: Iterable[str] | None = None,
    ) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        self.timeout_sec = float(timeout_sec)
        self.base_url = base_url.rstrip("/")
        parsed_base = urlparse(self.base_url)
        self.allowed_hosts = set(allowed_hosts or _DEFAULT_HOSTS)
        if parsed_base.hostname:
            self.allowed_hosts.add(parsed_base.hostname.casefold().rstrip("."))
        approved_url(self.base_url, self.allowed_hosts, "Chess.com Broadcast base URL")
        self.opener, self.cookie_jar = make_opener(opener, session)
        self._records: dict[str, dict[str, Any]] = {}
        self.last_download_filename: str | None = None
        self.last_download_content_type: str | None = None
        self.last_download_content_disposition: str | None = None
        self.last_download_status: int | None = None
        self.last_download_byte_count = 0
        self.last_download_looks_like_pgn = False

    @staticmethod
    def _archive_path(path: str) -> bool:
        lower = path.casefold()
        return any(
            marker in lower
            for marker in (
                "/games/archive",
                "/archive/",
                "/online/",
                "/play/",
                "/player/",
                "/user/",
            )
        )

    @staticmethod
    def _broadcast_path(path: str) -> bool:
        lower = path.casefold()
        return any(
            marker in lower
            for marker in ("/broadcast", "/event/", "/events/", "/api/broadcast", "/api/events")
        )

    def _page_url(self, event_id: str, game_id: str, source_url: str | None = None) -> str:
        if source_url:
            source_url = urljoin(self.base_url + "/", source_url)
            same_origin(self.base_url, source_url, self.allowed_hosts, "Chess.com source URL")
            return source_url
        return f"{self.base_url}/broadcast/{quote(event_id)}/games/{quote(game_id)}"

    def _pgn_url(self, event_id: str, game_id: str, source_url: str, candidate: str | None) -> str:
        if candidate:
            candidate = urljoin(source_url, candidate)
        elif urlparse(source_url).path.casefold().endswith(".pgn"):
            candidate = source_url
        else:
            candidate = f"{origin_base(source_url)}/broadcast/{quote(event_id)}/games/{quote(game_id)}.pgn"
        same_origin(source_url, candidate, self.allowed_hosts, "Chess.com PGN URL")
        found = _identity_from_url(candidate)
        if found is not None and found != (event_id, game_id):
            raise ValueError("Chess.com PGN URL identity does not match source ref")
        return candidate

    def _cache(self, event_id: str, game_id: str, mapping: dict[str, Any], source_url: str) -> None:
        candidate = _first(mapping, "pgn_url", "pgnUrl", "download_url", "downloadUrl")
        candidate = candidate.strip() if isinstance(candidate, str) and candidate.strip() else None
        self._records[f"event:{event_id}/game:{game_id}"] = {
            "event_id": event_id,
            "game_id": game_id,
            "mapping": dict(mapping),
            "source_url": source_url,
            "pgn_url": self._pgn_url(event_id, game_id, source_url, candidate),
        }

    def _refs_from_feed(self, payload: object, feed_url: str) -> list[SourceRef]:
        refs: list[SourceRef] = []
        seen: set[str] = set()
        for (event_id, game_id), mapping in _iter_games(
            payload, context_text=f"broadcast {feed_url}"
        ):
            source_value = _first(mapping, "source_url", "sourceUrl", "url", "game_url", "gameUrl")
            source_url = source_value.strip() if isinstance(source_value, str) and source_value.strip() else None
            if source_url:
                source_url = urljoin(feed_url, source_url)
            source_url = self._page_url(event_id, game_id, source_url)
            if self._archive_path(urlparse(source_url).path):
                continue
            self._cache(event_id, game_id, mapping, source_url)
            external_id = f"event:{event_id}/game:{game_id}"
            if external_id in seen:
                continue
            seen.add(external_id)
            refs.append(SourceRef(PROVIDER, external_id, source_url))
        return refs

    def discover(self, query: str) -> list[SourceRef]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Chess.com Broadcast discovery requires a structured feed or game URL")
        query = query.strip()
        if query.startswith("chesscom-broadcast:"):
            event_id, game_id = _parse_external_id(query.removeprefix("chesscom-broadcast:"))
            source_url = self._page_url(event_id, game_id)
            self._cache(event_id, game_id, {"otb": True, "broadcast": True}, source_url)
            return [SourceRef(PROVIDER, f"event:{event_id}/game:{game_id}", source_url)]
        if query.startswith("event:"):
            event_id, game_id = _parse_external_id(query)
            source_url = self._page_url(event_id, game_id)
            self._cache(event_id, game_id, {"otb": True, "broadcast": True}, source_url)
            return [SourceRef(PROVIDER, f"event:{event_id}/game:{game_id}", source_url)]

        if not (query.startswith("http://") or query.startswith("https://")):
            raise ValueError("Chess.com Broadcast discovery requires an absolute structured URL")
        approved_url(query, self.allowed_hosts, "Chess.com source URL")
        parsed = urlparse(query)
        if self._archive_path(parsed.path):
            raise ValueError("ordinary online archive paths are not broadcast sources")
        identity = _identity_from_url(query)
        if identity is not None:
            if not self._broadcast_path(parsed.path):
                raise ValueError("Chess.com source URL is not a structured broadcast path")
            event_id, game_id = identity
            self._cache(event_id, game_id, {"otb": True, "broadcast": True}, query)
            return [SourceRef(PROVIDER, f"event:{event_id}/game:{game_id}", query)]
        if not self._broadcast_path(parsed.path):
            raise ValueError("Chess.com source URL is not a structured broadcast path")
        fetched = fetch_body(
            self.opener,
            query,
            timeout=self.timeout_sec,
            allowed_hosts=self.allowed_hosts,
            user_agent=USER_AGENT,
            provider="Chess.com Broadcast",
        )
        payload = parse_json_body(fetched.body, "Chess.com Broadcast")
        refs = self._refs_from_feed(payload, fetched.url)
        if not refs:
            raise ValueError("Chess.com structured feed contained no OTB broadcast games")
        return refs

    def _validate_ref(self, ref: SourceRef) -> tuple[str, str]:
        if not isinstance(ref, SourceRef):
            raise TypeError("ref must be a SourceRef")
        if ref.provider != PROVIDER:
            raise ValueError("source ref provider is not chesscom-broadcast")
        event_id, game_id = _parse_external_id(ref.external_id)
        approved_url(ref.source_url, self.allowed_hosts, "Chess.com source URL")
        parsed = urlparse(ref.source_url)
        if self._archive_path(parsed.path):
            raise ValueError("ordinary online archive paths are not broadcast sources")
        found = _identity_from_url(ref.source_url)
        if found is not None and found != (event_id, game_id):
            raise ValueError("Chess.com source URL identity does not match source ref")
        return event_id, game_id

    def _record_from_page(self, ref: SourceRef, event_id: str, game_id: str) -> dict[str, Any]:
        fetched = fetch_body(
            self.opener,
            ref.source_url,
            timeout=self.timeout_sec,
            allowed_hosts=self.allowed_hosts,
            user_agent=USER_AGENT,
            provider="Chess.com Broadcast",
        )
        body_text = object_text(fetched.body)
        try:
            payload = parse_json_body(fetched.body, "Chess.com Broadcast")
        except ValueError:
            if not looks_like_html(fetched.body):
                raise ValueError(
                    "Chess.com result is neither structured JSON nor an HTML broadcast page"
                )
            if self._archive_path(urlparse(ref.source_url).path) or "online" in body_text.casefold():
                raise ValueError("Chess.com result is an online archive, not an OTB broadcast")
            parser = _TitleParser()
            parser.feed(body_text)
            mapping = {"title": parser.title, "otb": True, "broadcast": True}
        else:
            mapping = None
            for found, candidate in _iter_games(payload, context_text="broadcast"):
                if found == (event_id, game_id):
                    mapping = dict(candidate)
                    break
            if mapping is None and isinstance(payload, dict):
                found = _mapping_identity(payload)
                if found == (event_id, game_id):
                    mapping = dict(payload)
            if mapping is None:
                for found, _ in _iter_games(payload, context_text="broadcast"):
                    if found[0] == event_id:
                        raise ValueError("Chess.com result game identity does not match source ref")
                raise ValueError("Chess.com result game identity does not match source ref")
        if not _is_otb(mapping, "broadcast"):
            raise ValueError("Chess.com result is not an OTB broadcast game")
        self._cache(event_id, game_id, mapping, ref.source_url)
        return self._records[f"event:{event_id}/game:{game_id}"]

    def describe(self, ref: SourceRef) -> SourceDescriptor:
        event_id, game_id = self._validate_ref(ref)
        record = self._record_from_page(ref, event_id, game_id)
        mapping = record["mapping"]
        title_value = _first(mapping, "title", "name", "event_name", "eventName")
        title = title_value.strip() if isinstance(title_value, str) and title_value.strip() else None
        country_value = _first(mapping, "event_country", "eventCountry", "country")
        country = country_value.strip().upper() if isinstance(country_value, str) and country_value.strip() else None
        control_value = _first(mapping, "time_control", "timeControl", "format")
        control = control_value.strip().casefold() if isinstance(control_value, str) and control_value.strip() else None
        return SourceDescriptor(
            ref=ref,
            title=title,
            pgn_url=record["pgn_url"],
            event_country_hint=country,
            time_control_hint=control,
            is_otb_hint=True,
        )

    @staticmethod
    def _tag_values(prefix: bytes, names: set[str]) -> list[str]:
        text = prefix.decode("utf-8", errors="replace")
        return [
            match.group(2).strip()
            for match in re.finditer(r"(?m)^\s*\[([^\s]+)\s+\"([^\"]*)\"\]", text)
            if match.group(1).casefold() in names
        ]

    def _validate_download_identity(
        self,
        response: object,
        prefix: bytes,
        content_type_value: str | None,
        event_id: str,
        game_id: str,
        requested_url: str,
    ) -> None:
        ensure_pgn_signature(prefix, content_type_value, "Chess.com Broadcast")
        final_url = getattr(response, "geturl", lambda: "")() or requested_url
        url_identity = _identity_from_url(str(final_url))
        if url_identity is not None and url_identity != (event_id, game_id):
            raise ValueError("Chess.com PGN response identity does not match source ref")
        checks = {
            "event": (event_id, {"eventid", "event_id"}, ("X-Event-Id", "X-Chesscom-Event-Id")),
            "game": (game_id, {"gameid", "game_id"}, ("X-Game-Id", "X-Chesscom-Game-Id")),
        }
        for label, (expected, tag_names, header_names) in checks.items():
            values = self._tag_values(prefix, tag_names)
            if values and any(value != expected for value in values):
                raise ValueError(f"Chess.com PGN {label} identity does not match source ref")
            header_value = next(
                (response_header(response, name) for name in header_names if response_header(response, name) is not None),
                None,
            )
            if header_value is not None and header_value != expected:
                raise ValueError(f"Chess.com response {label} identity does not match source ref")

    def download_pgn(self, ref: SourceRef, destination: Path) -> Path:
        event_id, game_id = self._validate_ref(ref)
        key = f"event:{event_id}/game:{game_id}"
        record = self._records.get(key)
        if record is None:
            record = self._record_from_page(ref, event_id, game_id)
        if not _is_otb(record["mapping"], "broadcast"):
            raise ValueError("Chess.com result is not an OTB broadcast game")
        pgn_url = self._pgn_url(event_id, game_id, ref.source_url, record.get("pgn_url"))
        snapshot: ResponseSnapshot = stream_download(
            self.opener,
            pgn_url,
            Path(destination),
            timeout=self.timeout_sec,
            allowed_hosts=self.allowed_hosts,
            user_agent=USER_AGENT,
            provider="Chess.com Broadcast",
            response_validator=lambda response, prefix, content_type_value: self._validate_download_identity(
                response, prefix, content_type_value, event_id, game_id, pgn_url
            ),
        )
        self.last_download_filename = snapshot.filename
        self.last_download_content_type = snapshot.content_type
        self.last_download_content_disposition = snapshot.content_disposition
        self.last_download_status = snapshot.status
        self.last_download_byte_count = snapshot.byte_count
        self.last_download_looks_like_pgn = True
        return Path(destination)


Chess24Adapter = ChessComBroadcastAdapter
ChessComAdapter = ChessComBroadcastAdapter
ChessComChess24Adapter = ChessComBroadcastAdapter


__all__ = [
    "Chess24Adapter",
    "ChessComAdapter",
    "ChessComBroadcastAdapter",
    "ChessComChess24Adapter",
    "PROVIDER",
    "USER_AGENT",
]
