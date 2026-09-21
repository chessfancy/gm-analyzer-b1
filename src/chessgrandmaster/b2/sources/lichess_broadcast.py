"""Lichess Broadcast-only OTB source adapter."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urlencode, unquote, urljoin, urlparse

from ._common import (
    ResponseSnapshot,
    approved_url,
    ensure_pgn_signature,
    fetch_body,
    make_opener,
    looks_like_html,
    object_text,
    origin_base,
    parse_json_body,
    response_header,
    same_origin,
    stream_download,
)
from .base import SourceDescriptor, SourceRef


PROVIDER = "lichess-broadcast"
USER_AGENT = "ChessGrandmaster acquisition/1.0"
_DEFAULT_BASE_URL = "https://lichess.org"
_DEFAULT_HOSTS = {"lichess.org", "www.lichess.org"}
_ID_PART_RE = re.compile(r"^[^/\\?#\s]+$")
_EXTERNAL_RE = re.compile(
    r"^broadcast:(?P<broadcast>[^/]+)/event:(?P<event>[^/]+)/round:(?P<round>[^/]+)$"
)


@dataclass(frozen=True)
class _Identity:
    broadcast_id: str
    event_id: str
    round_id: str

    @property
    def external_id(self) -> str:
        return (
            f"broadcast:{self.broadcast_id}/event:{self.event_id}/"
            f"round:{self.round_id}"
        )


def _part(value: object, label: str) -> str | None:
    if not isinstance(value, str):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        else:
            return None
    value = value.strip()
    if not value or not _ID_PART_RE.fullmatch(value):
        return None
    return value


def _first(mapping: dict[str, Any], *names: str) -> object | None:
    lowered = {str(key).casefold(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.casefold())
        if value is not None:
            return value
    return None


def _nested_id(value: object) -> object:
    if isinstance(value, dict):
        for name in ("id", "key", "slug", "broadcast_id", "event_id", "round_id"):
            if name in value:
                return value[name]
    return value


def _bool_value(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "yes", "1", "otb", "over-the-board"}:
            return True
        if normalized in {"false", "no", "0", "online", "internet"}:
            return False
    return None


def _identity_from_mapping(
    mapping: dict[str, Any],
    context: _Identity | None = None,
) -> _Identity | None:
    broadcast = _part(
        _nested_id(_first(mapping, "broadcast_id", "broadcastId", "broadcast")),
        "broadcast id",
    )
    event = _part(
        _nested_id(_first(mapping, "event_id", "eventId", "event", "section")),
        "event id",
    )
    round_id = _part(
        _nested_id(_first(mapping, "round_id", "roundId", "round", "round_name")),
        "round id",
    )
    if broadcast is None and context is not None:
        broadcast = context.broadcast_id
    if event is None and context is not None:
        event = context.event_id
    if round_id is None and context is not None:
        round_id = context.round_id
    if broadcast is None or event is None or round_id is None:
        return None
    return _Identity(broadcast, event, round_id)


def _parse_external_id(value: str) -> _Identity:
    match = _EXTERNAL_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            "Lichess Broadcast external_id must identify broadcast, event, and round"
        )
    return _Identity(
        match.group("broadcast"), match.group("event"), match.group("round")
    )


def _identity_from_url(url: str) -> _Identity | None:
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query, keep_blank_values=True)
    broadcast = next(
        (query[name][0] for name in ("broadcast", "broadcast_id", "broadcastId") if query.get(name)),
        None,
    )
    event = next(
        (query[name][0] for name in ("event", "event_id", "eventId") if query.get(name)),
        None,
    )
    round_id = next(
        (query[name][0] for name in ("round", "round_id", "roundId") if query.get(name)),
        None,
    )
    if "broadcast" in parts:
        index = parts.index("broadcast")
        remaining = parts[index + 1 :]
        if len(remaining) >= 3:
            broadcast = broadcast or remaining[0]
            event = event or remaining[1]
            round_id = round_id or remaining[2]
    if len(parts) >= 4 and parts[-4:-2] == ["broadcast", "round"]:
        round_id = round_id or parts[-1].removesuffix(".pgn")
    if "round" in parts:
        index = parts.index("round")
        if index + 1 < len(parts):
            round_id = round_id or parts[index + 1].removesuffix(".pgn")
    if broadcast and event and round_id:
        values = (_part(broadcast, "broadcast"), _part(event, "event"), _part(round_id, "round"))
        if all(values):
            return _Identity(values[0], values[1], values[2])  # type: ignore[arg-type]
    return None


def _is_online(mapping: dict[str, Any], text: str) -> bool:
    for name in ("online", "is_online", "internet"):
        value = _bool_value(_first(mapping, name))
        if value is True:
            return True
    normalized = text.casefold()
    return bool(re.search(r"\b(?:online|internet|user\s+game)\b", normalized))


def _is_otb(mapping: dict[str, Any], context_text: str = "") -> bool:
    if _is_online(mapping, context_text):
        return False
    for name in ("otb", "is_otb", "over_the_board", "overTheBoard"):
        value = _bool_value(_first(mapping, name))
        if value is not None:
            return value
    text = " ".join(
        str(_first(mapping, name) or "")
        for name in ("title", "name", "format", "mode", "type", "event_type")
    ).casefold()
    if re.search(r"\bover[ -]the[ -]board\b|\botb\b", text):
        return True
    if re.search(r"\bonline\b|\binternet\b", text):
        return False
    # A record under the provider's broadcast route is not an ordinary user game.
    return bool(context_text and "broadcast" in context_text.casefold())


def _mapping_text(mapping: dict[str, Any]) -> str:
    return " ".join(str(value) for value in mapping.values() if isinstance(value, (str, int, float)))


def _iter_records(
    value: object,
    *,
    context: _Identity | None = None,
    broadcast_context: str | None = None,
    event_context: str | None = None,
    context_text: str = "",
):
    if isinstance(value, list):
        for item in value:
            yield from _iter_records(
                item,
                context=context,
                broadcast_context=broadcast_context,
                event_context=event_context,
                context_text=context_text,
            )
        return
    if not isinstance(value, dict):
        return
    own_broadcast = _part(
        _nested_id(_first(value, "broadcast_id", "broadcastId", "broadcast")),
        "broadcast id",
    ) or broadcast_context
    own_event = _part(
        _nested_id(_first(value, "event_id", "eventId", "event", "section")),
        "event id",
    ) or event_context
    own_round = _part(
        _nested_id(_first(value, "round_id", "roundId", "round", "round_name")),
        "round id",
    )
    identity = _identity_from_mapping(value, context)
    if identity is None and own_broadcast and own_event and own_round:
        identity = _Identity(own_broadcast, own_event, own_round)
    record_text = f"{context_text} {_mapping_text(value)}"
    if identity is not None and _is_otb(value, record_text):
        yield identity, value
    for key, child in value.items():
        if not isinstance(child, (dict, list)):
            continue
        child_text = f"{record_text} {key}"
        yield from _iter_records(
            child,
            context=identity or context,
            broadcast_context=own_broadcast,
            event_context=own_event,
            context_text=child_text,
        )


def _url_field(mapping: dict[str, Any], *names: str) -> str | None:
    value = _first(mapping, *names)
    return value.strip() if isinstance(value, str) and value.strip() else None


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


class LichessBroadcastAdapter:
    """Acquire explicit OTB Lichess Broadcast rounds as immutable PGN bytes."""

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
        approved_url(self.base_url, self.allowed_hosts, "Lichess base URL")
        self.opener, self.cookie_jar = make_opener(opener, session)
        self._records: dict[str, dict[str, Any]] = {}
        self.last_download_filename: str | None = None
        self.last_download_content_type: str | None = None
        self.last_download_content_disposition: str | None = None
        self.last_download_status: int | None = None
        self.last_download_byte_count = 0
        self.last_download_looks_like_pgn = False

    def _page_url(self, identity: _Identity, source_url: str | None = None) -> str:
        if source_url:
            source_url = urljoin(self.base_url + "/", source_url)
            same_origin(self.base_url, source_url, self.allowed_hosts, "Lichess source URL")
            return source_url
        return (
            f"{self.base_url}/broadcast/{quote(identity.broadcast_id)}/"
            f"{quote(identity.event_id)}/{quote(identity.round_id)}"
        )

    def _pgn_url(self, identity: _Identity, source_url: str, candidate: str | None) -> str:
        if candidate:
            candidate = urljoin(source_url, candidate)
        elif urlparse(source_url).path.casefold().endswith(".pgn"):
            candidate = source_url
        else:
            candidate = (
                f"{origin_base(source_url)}/api/broadcast/round/{quote(identity.round_id)}.pgn?"
                + urlencode(
                    {
                        "broadcast": identity.broadcast_id,
                        "event": identity.event_id,
                    }
                )
            )
        same_origin(source_url, candidate, self.allowed_hosts, "Lichess PGN URL")
        found = _identity_from_url(candidate)
        if found is not None:
            if found.round_id != identity.round_id:
                raise ValueError("Lichess PGN URL round identity does not match source ref")
            if found.broadcast_id != identity.broadcast_id or found.event_id != identity.event_id:
                raise ValueError("Lichess PGN URL event identity does not match source ref")
        return candidate

    def _cache(self, identity: _Identity, mapping: dict[str, Any], source_url: str) -> None:
        candidate = _url_field(mapping, "pgn_url", "pgnUrl", "download_url", "downloadUrl")
        self._records[identity.external_id] = {
            "identity": identity,
            "mapping": dict(mapping),
            "source_url": source_url,
            "pgn_url": self._pgn_url(identity, source_url, candidate),
        }

    def _refs_from_feed(self, payload: object, feed_url: str) -> list[SourceRef]:
        refs: list[SourceRef] = []
        seen: set[str] = set()
        for identity, mapping in _iter_records(
            payload, context_text=f"broadcast {feed_url}"
        ):
            source_url = _url_field(mapping, "source_url", "sourceUrl", "url", "round_url", "roundUrl")
            if source_url:
                source_url = urljoin(feed_url, source_url)
            source_url = self._page_url(identity, source_url)
            self._cache(identity, mapping, source_url)
            if identity.external_id in seen:
                continue
            seen.add(identity.external_id)
            refs.append(SourceRef(PROVIDER, identity.external_id, source_url))
        return refs

    def _explicit_query(self, query: str) -> tuple[_Identity, str] | None:
        if query.startswith("lichess-broadcast:"):
            tail = query.removeprefix("lichess-broadcast:")
            try:
                return _parse_external_id(tail), self._page_url(_parse_external_id(tail))
            except ValueError:
                parts = [part for part in tail.split("/") if part]
                if len(parts) == 3:
                    values = [_part(part.split(":", 1)[-1], "identity") for part in parts]
                    if all(values):
                        identity = _Identity(values[0], values[1], values[2])  # type: ignore[arg-type]
                        return identity, self._page_url(identity)
                raise
        if query.startswith("broadcast:"):
            identity = _parse_external_id(query)
            return identity, self._page_url(identity)
        if query.startswith("http://") or query.startswith("https://"):
            approved_url(query, self.allowed_hosts, "Lichess source URL")
            identity = _identity_from_url(query)
            if identity is not None:
                return identity, query
        return None

    def discover(self, query: str) -> list[SourceRef]:
        """Discover explicit rounds, or fetch one structured broadcast feed."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Lichess Broadcast discovery requires a feed or round URL")
        query = query.strip()
        explicit = self._explicit_query(query)
        if explicit is not None:
            identity, source_url = explicit
            if "/api/games/" in urlparse(source_url).path or "/game/" in urlparse(source_url).path:
                raise ValueError("Lichess ordinary online game paths are not broadcast sources")
            if identity.external_id not in self._records:
                self._cache(identity, {"otb": True}, source_url)
            return [SourceRef(PROVIDER, identity.external_id, source_url)]

        approved_url(query, self.allowed_hosts, "Lichess feed URL")
        parsed = urlparse(query)
        if "/broadcast" not in parsed.path.casefold():
            raise ValueError("Lichess source must use a Broadcast path")
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            "api" in {part.casefold() for part in path_parts}
            and "round" in {part.casefold() for part in path_parts}
            and _identity_from_url(query) is None
        ):
            raise ValueError(
                "Lichess round URL must include broadcast, event, and round identity"
            )
        if (
            path_parts
            and path_parts[0].casefold() == "broadcast"
            and len(path_parts) == 3
        ):
            raise ValueError("Lichess broadcast URL does not identify a round")
        fetched = fetch_body(
            self.opener,
            query,
            timeout=self.timeout_sec,
            allowed_hosts=self.allowed_hosts,
            user_agent=USER_AGENT,
            provider="Lichess Broadcast",
        )
        payload = parse_json_body(fetched.body, "Lichess Broadcast")
        refs = self._refs_from_feed(payload, fetched.url)
        if not refs:
            raise ValueError("Lichess feed contained no OTB broadcast rounds")
        return refs

    def _validate_ref(self, ref: SourceRef) -> _Identity:
        if not isinstance(ref, SourceRef):
            raise TypeError("ref must be a SourceRef")
        if ref.provider != PROVIDER:
            raise ValueError("source ref provider is not lichess-broadcast")
        identity = _parse_external_id(ref.external_id)
        approved_url(ref.source_url, self.allowed_hosts, "Lichess source URL")
        from_url = _identity_from_url(ref.source_url)
        if from_url is not None and from_url != identity:
            raise ValueError("Lichess source URL identity does not match source ref")
        return identity

    def _record_from_page(self, ref: SourceRef, identity: _Identity) -> dict[str, Any]:
        fetched = fetch_body(
            self.opener,
            ref.source_url,
            timeout=self.timeout_sec,
            allowed_hosts=self.allowed_hosts,
            user_agent=USER_AGENT,
            provider="Lichess Broadcast",
        )
        body_text = object_text(fetched.body)
        try:
            payload = parse_json_body(fetched.body, "Lichess Broadcast")
        except ValueError:
            # Public broadcast pages are HTML; the explicit URL still carries the
            # round identity, while online/user paths were rejected in discover.
            if not looks_like_html(fetched.body):
                raise ValueError(
                    "Lichess result is neither structured JSON nor an HTML broadcast page"
                )
            if "online" in body_text.casefold() and "over-the-board" not in body_text.casefold():
                raise ValueError("Lichess result is marked online, not OTB")
            parser = _TitleParser()
            parser.feed(body_text)
            mapping = {"title": parser.title, "otb": True}
        else:
            mapping = None
            for found, candidate in _iter_records(payload, context=identity, context_text="broadcast"):
                if found == identity:
                    mapping = dict(candidate)
                    break
            if mapping is None and isinstance(payload, dict):
                candidate_identity = _identity_from_mapping(payload)
                if candidate_identity == identity:
                    mapping = dict(payload)
            if mapping is None:
                for found, _ in _iter_records(payload, context_text="broadcast"):
                    if (
                        found.broadcast_id == identity.broadcast_id
                        and found.round_id == identity.round_id
                    ):
                        raise ValueError("Lichess result event identity does not match source ref")
                raise ValueError("Lichess result identity does not match source ref")
        if not _is_otb(mapping, "broadcast"):
            raise ValueError("Lichess result is not an OTB broadcast")
        self._cache(identity, mapping, ref.source_url)
        return self._records[identity.external_id]

    def describe(self, ref: SourceRef) -> SourceDescriptor:
        identity = self._validate_ref(ref)
        record = self._record_from_page(ref, identity)
        mapping = record["mapping"]
        title = _url_field(mapping, "title", "name", "event_name", "eventName")
        country = _url_field(mapping, "event_country", "eventCountry", "country")
        time_control = _url_field(mapping, "time_control", "timeControl", "format")
        return SourceDescriptor(
            ref=ref,
            title=title,
            pgn_url=record["pgn_url"],
            event_country_hint=country.upper() if country else None,
            time_control_hint=time_control.casefold() if time_control else None,
            is_otb_hint=True,
        )

    @staticmethod
    def _tag_values(prefix: bytes, names: set[str]) -> list[str]:
        text = prefix.decode("utf-8", errors="replace")
        values: list[str] = []
        for match in re.finditer(r"(?m)^\s*\[([^\s]+)\s+\"([^\"]*)\"\]", text):
            if match.group(1).casefold() in names:
                values.append(match.group(2).strip())
        return values

    def _validate_download_identity(
        self,
        response: object,
        prefix: bytes,
        content_type_value: str | None,
        identity: _Identity,
        requested_url: str,
    ) -> None:
        ensure_pgn_signature(prefix, content_type_value, "Lichess Broadcast")
        final_url = getattr(response, "geturl", lambda: "")() or requested_url
        url_identity = _identity_from_url(str(final_url))
        if url_identity is not None and url_identity != identity:
            raise ValueError("Lichess PGN response identity does not match source ref")
        aliases = {
            "broadcast": {"broadcastid", "broadcast_id"},
            "event": {"eventid", "event_id"},
            "round": {"roundid", "round_id"},
        }
        expected = {
            "broadcast": identity.broadcast_id,
            "event": identity.event_id,
            "round": identity.round_id,
        }
        for label, names in aliases.items():
            values = self._tag_values(prefix, names)
            if values and any(value != expected[label] for value in values):
                raise ValueError(f"Lichess PGN {label} identity does not match source ref")
            header_value = next(
                (
                    response_header(response, name)
                    for name in (
                        f"X-Broadcast-{label.title()}",
                        f"X-Lichess-Broadcast-{label.title()}",
                    )
                    if response_header(response, name) is not None
                ),
                None,
            )
            if header_value is not None and header_value != expected[label]:
                raise ValueError(f"Lichess response {label} identity does not match source ref")

    def download_pgn(self, ref: SourceRef, destination: Path) -> Path:
        identity = self._validate_ref(ref)
        record = self._records.get(identity.external_id)
        if record is None:
            record = self._record_from_page(ref, identity)
        mapping = record["mapping"]
        if not _is_otb(mapping, "broadcast"):
            raise ValueError("Lichess result is not an OTB broadcast")
        pgn_url = self._pgn_url(identity, ref.source_url, record.get("pgn_url"))

        snapshot: ResponseSnapshot = stream_download(
            self.opener,
            pgn_url,
            Path(destination),
            timeout=self.timeout_sec,
            allowed_hosts=self.allowed_hosts,
            user_agent=USER_AGENT,
            provider="Lichess Broadcast",
            response_validator=lambda response, prefix, content_type_value: self._validate_download_identity(
                response, prefix, content_type_value, identity, pgn_url
            ),
        )
        self.last_download_filename = snapshot.filename
        self.last_download_content_type = snapshot.content_type
        self.last_download_content_disposition = snapshot.content_disposition
        self.last_download_status = snapshot.status
        self.last_download_byte_count = snapshot.byte_count
        self.last_download_looks_like_pgn = True
        return Path(destination)


# Small compatibility aliases keep provider naming explicit for callers.
LichessAdapter = LichessBroadcastAdapter


__all__ = [
    "LichessAdapter",
    "LichessBroadcastAdapter",
    "PROVIDER",
    "USER_AGENT",
]
