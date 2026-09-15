"""Chess-Results source adapter using the public game-database workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import os
from pathlib import Path
import re
import socket
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .base import SourceDescriptor, SourceRef


PROVIDER = "chess-results"
USER_AGENT = "ChessGrandmaster acquisition/1.0"
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_PGN_PROBE_BYTES = 64 * 1024
_LITERAL_TNR_RE = re.compile(r"^tnr(?P<number>\d+)$", re.IGNORECASE)
_PATH_TNR_RE = re.compile(r"^tnr(?P<number>\d+)\.aspx$", re.IGNORECASE)
_NUMBERED_HOST_RE = re.compile(r"^s\d+\.chess-results\.com$", re.IGNORECASE)
_COUNTRY_RE = re.compile(
    r"\b(?:event\s+country|country|nation)\s*[:=\-]\s*([A-Za-z]{2,3})\b",
    re.IGNORECASE,
)
_TIME_CONTROL_RE = re.compile(
    r"\b(?:time\s*control|format|discipline)\s*[:=\-]\s*"
    r"(classical|standard|rapid|blitz|bullet|correspondence)\b",
    re.IGNORECASE,
)
_OTB_RE = re.compile(
    r"\b(?:format|mode|type|location)\s*[:=\-]\s*"
    r"(otb|over\s+the\s+board|online|internet)\b",
    re.IGNORECASE,
)
_ROUND_WORDS = {"round", "rd"}
_ROUND_FROM_WORDS = {"from", "start", "min", "first", "von"}
_ROUND_TO_WORDS = {"to", "end", "max", "last", "bis"}


@dataclass(frozen=True)
class _Anchor:
    href: str | None
    text: str


@dataclass(frozen=True)
class _FormControl:
    name: str
    value: str
    kind: str
    input_type: str = ""
    text: str = ""
    attributes: tuple[tuple[str, str], ...] = ()
    successful: bool = True

    @property
    def semantic_text(self) -> str:
        attrs = dict(self.attributes)
        return " ".join(
            (
                self.name,
                self.value,
                self.text,
                attrs.get("id", ""),
                attrs.get("class", ""),
                attrs.get("title", ""),
                attrs.get("aria-label", ""),
                attrs.get("placeholder", ""),
            )
        ).casefold()


@dataclass(frozen=True)
class _HtmlForm:
    action: str | None
    method: str
    controls: tuple[_FormControl, ...]

    @property
    def submit_controls(self) -> tuple[_FormControl, ...]:
        return tuple(
            control
            for control in self.controls
            if control.successful
            and control.kind == "submit"
            and control.input_type not in {"reset", "button"}
        )


@dataclass(frozen=True)
class _TableRow:
    cells: tuple[str, ...]


@dataclass
class _FormBuilder:
    action: str | None
    method: str
    controls: list[_FormControl] = field(default_factory=list)


@dataclass
class _OpenControl:
    name: str
    value: str
    input_type: str
    attributes: tuple[tuple[str, str], ...]
    parts: list[str] = field(default_factory=list)


@dataclass
class _OpenOption:
    value: str
    selected: bool
    parts: list[str] = field(default_factory=list)


@dataclass
class _OpenSelect:
    name: str
    attributes: tuple[tuple[str, str], ...]
    first_value: str | None = None
    selected_value: str | None = None
    option: _OpenOption | None = None


class _PageParser(HTMLParser):
    """Collect visible evidence, forms, and result-table rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self.page_titles: list[str] = []
        self.anchors: list[_Anchor] = []
        self.forms: list[_HtmlForm] = []
        self.table_rows: list[_TableRow] = []
        self._visible_parts: list[str] = []
        self._capture_stack: list[tuple[str, list[str]]] = []
        self._anchor_stack: list[tuple[str | None, list[str]]] = []
        self._form: _FormBuilder | None = None
        self._button_stack: list[_OpenControl] = []
        self._select_stack: list[_OpenSelect] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._ignored_depth = 0

    @staticmethod
    def _normalize_text(parts: Iterable[str]) -> str:
        return " ".join(" ".join(parts).split())

    @staticmethod
    def _attrs_tuple(
        attrs: list[tuple[str, str | None]],
    ) -> tuple[tuple[str, str], ...]:
        return tuple((name.casefold(), value or "") for name, value in attrs)

    def _finish_button(self) -> None:
        if self._form is None or not self._button_stack:
            return
        button = self._button_stack.pop()
        if not button.name or button.input_type in {"reset", "button"}:
            return
        text = self._normalize_text(button.parts)
        self._form.controls.append(
            _FormControl(
                name=button.name,
                value=button.value or text,
                kind="submit",
                input_type=button.input_type,
                text=text,
                attributes=button.attributes,
            )
        )

    @staticmethod
    def _finish_option_for(select: _OpenSelect) -> None:
        option = select.option
        if option is None:
            return
        text = " ".join(" ".join(option.parts).split())
        value = option.value or text
        if select.first_value is None:
            select.first_value = value
        if option.selected:
            select.selected_value = value
        select.option = None

    def _finish_option(self) -> None:
        if self._select_stack:
            self._finish_option_for(self._select_stack[-1])

    def _finish_select(self) -> None:
        if self._form is None or not self._select_stack:
            return
        select = self._select_stack.pop()
        self._finish_option_for(select)
        value = select.selected_value or select.first_value or ""
        attrs = dict(select.attributes)
        if select.name:
            self._form.controls.append(
                _FormControl(
                    name=select.name,
                    value=value,
                    kind="select",
                    input_type="select",
                    attributes=select.attributes,
                    successful="disabled" not in attrs,
                )
            )

    def _finish_form(self) -> None:
        if self._form is None:
            return
        while self._button_stack:
            self._finish_button()
        while self._select_stack:
            self._finish_select()
        form = self._form
        self.forms.append(
            _HtmlForm(
                action=form.action,
                method=form.method or "GET",
                controls=tuple(form.controls),
            )
        )
        self._form = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return

        attributes = dict(self._attrs_tuple(attrs))
        if tag in {"title", "h1", "h2"}:
            self._capture_stack.append((tag, []))
        if tag == "a":
            self._anchor_stack.append((attributes.get("href"), []))

        if tag == "form":
            self._finish_form()
            self._form = _FormBuilder(
                action=attributes.get("action"),
                method=attributes.get("method", "GET").upper(),
            )
        elif tag == "input" and self._form is not None:
            name = attributes.get("name", "")
            input_type = attributes.get("type", "text").casefold()
            if name and input_type not in {"reset", "button"}:
                successful = not (
                    input_type in {"checkbox", "radio"}
                    and "checked" not in attributes
                ) and "disabled" not in attributes
                self._form.controls.append(
                    _FormControl(
                        name=name,
                        value=attributes.get("value", ""),
                        kind=(
                            "submit"
                            if input_type in {"submit", "image"}
                            else "input"
                        ),
                        input_type=input_type,
                        attributes=self._attrs_tuple(attrs),
                        successful=successful,
                    )
                )
        elif tag == "button" and self._form is not None:
            self._button_stack.append(
                _OpenControl(
                    name=attributes.get("name", ""),
                    value=attributes.get("value", ""),
                    input_type=attributes.get("type", "submit").casefold(),
                    attributes=self._attrs_tuple(attrs),
                )
            )
        elif tag == "select" and self._form is not None:
            self._select_stack.append(
                _OpenSelect(
                    name=attributes.get("name", ""),
                    attributes=self._attrs_tuple(attrs),
                )
            )
        elif tag == "option" and self._select_stack:
            self._finish_option()
            self._select_stack[-1].option = _OpenOption(
                value=attributes.get("value", ""),
                selected="selected" in attributes,
            )

        if tag == "tr":
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return

        if tag == "a" and self._anchor_stack:
            href, parts = self._anchor_stack.pop()
            self.anchors.append(_Anchor(href, self._normalize_text(parts)))
        elif tag == "button":
            self._finish_button()
        elif tag == "option":
            self._finish_option()
        elif tag == "select":
            self._finish_select()
        elif tag in {"th", "td"} and self._row is not None:
            self._row.append(self._normalize_text(self._cell or []))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.table_rows.append(_TableRow(tuple(self._row)))
            self._row = None
        elif tag == "form":
            self._finish_form()

        for index in range(len(self._capture_stack) - 1, -1, -1):
            capture_tag, parts = self._capture_stack[index]
            if capture_tag != tag:
                continue
            self._capture_stack.pop(index)
            text = self._normalize_text(parts)
            if not text:
                break
            if tag in {"h1", "h2"}:
                self.headings.append(text)
            else:
                self.page_titles.append(text)
            break

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self._visible_parts.append(data)
        for _, parts in self._capture_stack:
            parts.append(data)
        for _, parts in self._anchor_stack:
            parts.append(data)
        if self._button_stack:
            self._button_stack[-1].parts.append(data)
        if self._select_stack and self._select_stack[-1].option is not None:
            self._select_stack[-1].option.parts.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def finish(self) -> None:
        """Retain useful text when malformed HTML omits closing tags."""
        self._finish_form()
        if self._row is not None:
            if self._cell is not None:
                self._row.append(self._normalize_text(self._cell))
            self.table_rows.append(_TableRow(tuple(self._row)))
            self._row = None
            self._cell = None
        for tag, parts in self._capture_stack:
            text = self._normalize_text(parts)
            if not text:
                continue
            if tag in {"h1", "h2"}:
                self.headings.append(text)
            else:
                self.page_titles.append(text)
        for href, parts in self._anchor_stack:
            self.anchors.append(_Anchor(href, self._normalize_text(parts)))
        self._capture_stack.clear()
        self._anchor_stack.clear()

    @property
    def visible_text(self) -> str:
        return self._normalize_text(self._visible_parts)


@dataclass(frozen=True)
class _FetchedPage:
    url: str
    html: str


def _supported_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.casefold().rstrip(".")
    return normalized in {
        "chess-results.com",
        "www.chess-results.com",
    } or bool(_NUMBERED_HOST_RE.fullmatch(normalized))


def _bare_provider_host(host: str | None) -> bool:
    if not host:
        return False
    return host.casefold().rstrip(".") in {
        "chess-results.com",
        "www.chess-results.com",
    }


def _parsed_port(parsed) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme.casefold() == "https" else 80


def _approved_url(url: str, description: str) -> str:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(
            f"Chess-Results {description} is not a valid URL"
        ) from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise RuntimeError(f"Chess-Results {description} is not an HTTP URL")
    if has_credentials or not _supported_host(hostname):
        raise RuntimeError(
            f"Chess-Results {description} is outside approved source hosts"
        )
    return url


def _same_origin(
    base_url: str,
    candidate: str,
    description: str,
    *,
    allow_approved_shard_redirect: bool = False,
) -> str:
    _approved_url(candidate, description)
    base = urlparse(base_url)
    target = urlparse(candidate)
    same_origin = (
        base.scheme.casefold(),
        (base.hostname or "").casefold().rstrip("."),
        _parsed_port(base),
    ) == (
        target.scheme.casefold(),
        (target.hostname or "").casefold().rstrip("."),
        _parsed_port(target),
    )
    if same_origin:
        return candidate
    if (
        allow_approved_shard_redirect
        and base.scheme.casefold() == target.scheme.casefold()
        and _parsed_port(base) == _parsed_port(target)
        and _bare_provider_host(base.hostname)
        and bool(_NUMBERED_HOST_RE.fullmatch((target.hostname or "").casefold()))
    ):
        return candidate
    if not same_origin:
        raise RuntimeError(f"Chess-Results {description} is cross-origin")
    return candidate


def _external_id_from_url(source_url: str) -> str:
    try:
        parsed = urlparse(source_url)
        hostname = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Chess-Results source URL is not valid") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("Chess-Results source URL must use http or https")
    if has_credentials or not _supported_host(hostname):
        raise ValueError("source URL is not a supported Chess-Results host")
    match = _PATH_TNR_RE.fullmatch(parsed.path.rsplit("/", 1)[-1])
    if match is None:
        raise ValueError("source URL must end with tnrNNNN.aspx")
    return f"tnr{match.group('number')}"


def database_key_from_ref(ref: SourceRef) -> str:
    """Derive the Chess-Results database key from a validated-style ref."""
    if not isinstance(ref, SourceRef):
        raise TypeError("ref must be a SourceRef")
    if ref.provider != PROVIDER:
        raise ValueError("source ref provider is not chess-results")
    match = _LITERAL_TNR_RE.fullmatch(ref.external_id)
    if match is None:
        raise ValueError("Chess-Results external_id must match tnrNNNN")
    return match.group("number")


def _pgn_candidate(page_url: str, anchor: _Anchor) -> str | None:
    if not anchor.href:
        return None
    candidate = urljoin(page_url, anchor.href)
    try:
        _same_origin(page_url, candidate, "PGN link")
    except RuntimeError:
        return None
    parsed = urlparse(candidate)
    text = anchor.text.casefold()
    exposed_url_text = unquote(f"{parsed.path}?{parsed.query}").casefold()
    if (
        parsed.path.casefold().endswith(".pgn")
        or text.endswith(".pgn")
        or ".pgn" in exposed_url_text
        or re.search(r"\bpgn\b", text)
    ):
        return candidate
    return None


def _metadata_hints(text: str) -> tuple[str | None, str | None, bool | None]:
    country_match = _COUNTRY_RE.search(text)
    event_country = (
        country_match.group(1).upper() if country_match is not None else None
    )

    time_control_match = _TIME_CONTROL_RE.search(text)
    time_control = (
        time_control_match.group(1).casefold()
        if time_control_match is not None
        else None
    )

    otb_match = _OTB_RE.search(text)
    is_otb: bool | None = None
    if otb_match is not None:
        is_otb = otb_match.group(1).casefold() in {"otb", "over the board"}

    return event_country, time_control, is_otb


def _parse_page(html: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(html)
        parser.close()
        parser.finish()
    except Exception as exc:
        raise RuntimeError("Chess-Results page is malformed") from exc
    if not parser.visible_text and not parser.anchors and not parser.forms:
        raise RuntimeError("Chess-Results page is empty or malformed")
    return parser


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _database_key_score(control: _FormControl, *, allow_hidden: bool) -> int:
    if not control.successful or control.kind == "submit":
        return 0
    if not allow_hidden and control.input_type == "hidden":
        return 0
    semantic = control.semantic_text
    words = _words(semantic)
    compact = _compact(semantic)
    if "dbkey" in compact or "databasekey" in compact:
        return 100
    if "database" in words and "key" in words:
        return 95
    if "tournament" in words and "key" in words:
        return 90
    if ("database" in words or "db" in words) and words & {
        "id",
        "identifier",
        "reference",
        "number",
    }:
        return 70
    return 0


def _database_key_control(
    form: _HtmlForm,
    *,
    allow_hidden: bool = False,
) -> _FormControl:
    scored = [
        (score, control)
        for control in form.controls
        if (score := _database_key_score(control, allow_hidden=allow_hidden))
    ]
    if not scored:
        raise RuntimeError(
            "Chess-Results search form exposes no database-key input"
        )
    best_score = max(score for score, _ in scored)
    best = [control for score, control in scored if score == best_score]
    if len(best) != 1:
        raise RuntimeError("Chess-Results form has ambiguous database-key inputs")
    return best[0]


def _search_submit(form: _HtmlForm) -> _FormControl:
    candidates = []
    for control in form.submit_controls:
        words = _words(control.semantic_text)
        if "pgn" in words or "download" in words or "export" in words:
            continue
        if words & {"search", "suchen", "find", "query"}:
            candidates.append(control)
    if len(candidates) != 1:
        raise RuntimeError("Chess-Results search form exposes no unique search submit")
    return candidates[0]


def _search_form(
    parser: _PageParser,
) -> tuple[_HtmlForm, _FormControl, _FormControl]:
    for form in parser.forms:
        try:
            key_control = _database_key_control(form)
            search_submit = _search_submit(form)
        except RuntimeError:
            continue
        return form, key_control, search_submit
    raise RuntimeError("Chess-Results page exposes no usable search form")


def _download_form(parser: _PageParser) -> tuple[_HtmlForm, _FormControl]:
    for form in parser.forms:
        for control in form.submit_controls:
            semantic = control.semantic_text
            words = _words(semantic)
            if "pgn" in words and words & {"download", "export", "get"}:
                return form, control
    raise RuntimeError(
        "Chess-Results result page exposes no Download as PGN-File form"
    )


def _is_round_control(control: _FormControl) -> bool:
    semantic = control.semantic_text
    words = _words(semantic)
    if words & _ROUND_WORDS or "round" in semantic:
        return True
    return bool(
        re.search(
            r"rd(?:from|to|von|bis|start|end|min|max|first|last)",
            _compact(semantic),
        )
    )


def _has_round_side(control: _FormControl, side_words: set[str]) -> bool:
    semantic = control.semantic_text
    if _words(semantic) & side_words:
        return True
    compact = _compact(semantic)
    return any(f"rd{word}" in compact for word in side_words)


def _round_controls(form: _HtmlForm) -> tuple[_FormControl, _FormControl]:
    candidates = [
        control
        for control in form.controls
        if control.successful
        and control.kind != "submit"
        and control.input_type != "hidden"
        and _is_round_control(control)
    ]
    from_controls = [
        control
        for control in candidates
        if _has_round_side(control, _ROUND_FROM_WORDS)
    ]
    to_controls = [
        control
        for control in candidates
        if _has_round_side(control, _ROUND_TO_WORDS)
    ]
    if not from_controls or not to_controls:
        raise RuntimeError(
            "Chess-Results download form exposes no round-from/round-to controls"
        )
    if from_controls[0].name == to_controls[0].name:
        raise RuntimeError("Chess-Results round controls are ambiguous")
    return from_controls[0], to_controls[0]


def _header_indices(row: _TableRow) -> tuple[int | None, int | None]:
    normalized = [_compact(cell) for cell in row.cells]
    key_index = next(
        (
            index
            for index, cell in enumerate(normalized)
            if cell in {"dbkey", "databasekey", "tournamentkey"}
            or ("database" in cell and "key" in cell)
        ),
        None,
    )
    round_index = next(
        (
            index
            for index, cell in enumerate(normalized)
            if cell in {"rd", "round", "roundnumber"}
            or cell.startswith("round")
        ),
        None,
    )
    return key_index, round_index


def _integer_cell(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*", value)
    return int(match.group(1)) if match is not None else None


def _rounds_from_rows(
    rows: list[_TableRow],
    requested_key: str | None = None,
) -> tuple[tuple[int, int], bool]:
    for header_index, header in enumerate(rows):
        key_index, round_index = _header_indices(header)
        if key_index is None or round_index is None:
            continue
        counts: dict[int, int] = {}
        matched = False
        for data_row in rows[header_index + 1 :]:
            if _header_indices(data_row) == (key_index, round_index):
                break
            if len(data_row.cells) <= max(key_index, round_index):
                continue
            row_key = data_row.cells[key_index].strip()
            if not re.fullmatch(r"\d+", row_key):
                continue
            if requested_key is not None and row_key != requested_key:
                continue
            round_number = _integer_cell(data_row.cells[round_index])
            if round_number is None or round_number <= 0:
                continue
            matched = True
            counts[round_number] = counts.get(round_number, 0) + 1
        if counts:
            return tuple(sorted(counts.items())), matched
    return (), False


def _available_rounds(parser: _PageParser) -> tuple[tuple[int, int], ...]:
    """Return ``(round_number, game_count)`` pairs from result rows."""
    rounds, _ = _rounds_from_rows(parser.table_rows)
    return rounds


def _parse_database_result(
    parser: _PageParser,
    requested_key: str,
) -> tuple[tuple[int, int], ...]:
    rounds, matched = _rounds_from_rows(parser.table_rows, requested_key)
    if not matched:
        raise RuntimeError(
            f"Chess-Results result does not identify database key {requested_key}"
        )
    if not rounds:
        raise RuntimeError(
            f"Chess-Results result for database key {requested_key} has no rounds"
        )
    return rounds


def _response_status(response: object) -> int | None:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            status = getcode()
    return int(status) if status is not None else None


def _response_header(response: object, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        info = getattr(response, "info", None)
        if callable(info):
            headers = info()
    if headers is not None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            value = getter(name)
            if value is None:
                value = getter(name.lower())
            if value is not None:
                return str(value)
        for key, value in getattr(headers, "items", lambda: ())():
            if str(key).casefold() == name.casefold():
                return str(value)
    getheader = getattr(response, "getheader", None)
    if callable(getheader):
        value = getheader(name)
        if value is not None:
            return str(value)
    return None


def _content_type(response: object) -> str | None:
    value = _response_header(response, "Content-Type")
    if value is None:
        return None
    return value.split(";", 1)[0].strip().casefold() or None


def _content_disposition_filename(response: object) -> str | None:
    value = _response_header(response, "Content-Disposition")
    if value is None:
        return None
    match = re.search(
        r"filename\*?\s*=\s*(?:UTF-8''([^;]+)|\"([^\"]+)\"|([^;\s]+))",
        value,
        re.IGNORECASE,
    )
    if match is None:
        return None
    filename = next((part for part in match.groups() if part is not None), "")
    return unquote(filename.strip().strip('"')) or None


def _looks_like_html(prefix: bytes) -> bool:
    text = prefix.decode("utf-8", errors="replace").lstrip("\ufeff \t\r\n").casefold()
    return bool(
        text.startswith("<!doctype html")
        or text.startswith("<html")
        or text.startswith("<body")
        or "<html" in text[:512]
    )


def _looks_like_pgn(prefix: bytes) -> bool:
    text = prefix.decode("utf-8", errors="replace")
    if re.search(r"(?m)^\s*\[[A-Za-z][A-Za-z0-9_]*\s+\"[^\"]*\"\]", text):
        return True
    return bool(re.search(r"(?m)^\s*\d+\.(?:\.\.)?\s+\S+", text))


class ChessResultsAdapter:
    """Acquire Chess-Results PGN through the provider's exposed forms."""

    def __init__(self, timeout_sec: float = 30.0, opener: Any = None) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        self.timeout_sec = float(timeout_sec)
        self.cookie_jar: CookieJar | None = None
        if opener is None:
            self.cookie_jar = CookieJar()
            self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        else:
            self.opener = opener
        self.last_download_filename: str | None = None
        self.last_download_content_type: str | None = None
        self.last_download_content_disposition: str | None = None
        self.last_download_status: int | None = None
        self.last_download_byte_count = 0
        self.last_download_looks_like_pgn = False

    def discover(self, query: str) -> list[SourceRef]:
        """Normalize one literal tnr reference or supported absolute URL."""
        if not isinstance(query, str) or not query:
            raise ValueError(
                "Chess-Results discovery accepts tnrNNNN or an absolute source URL"
            )

        literal_match = _LITERAL_TNR_RE.fullmatch(query)
        if literal_match is not None:
            external_id = f"tnr{literal_match.group('number')}"
            source_url = f"https://chess-results.com/{external_id}.aspx"
        else:
            external_id = _external_id_from_url(query)
            source_url = query

        return [SourceRef(PROVIDER, external_id, source_url)]

    def _validate_ref(self, ref: SourceRef) -> SourceRef:
        if not isinstance(ref, SourceRef):
            raise TypeError("ref must be a SourceRef")
        key = database_key_from_ref(ref)
        if _external_id_from_url(ref.source_url).casefold() != f"tnr{key}".casefold():
            raise ValueError("source ref external_id does not match source_url")
        return ref

    @staticmethod
    def _search_page_url(ref: SourceRef) -> str:
        parsed = urlparse(ref.source_url)
        return urlunparse(
            parsed._replace(path="/partiesuche.aspx", params="", query="", fragment="")
        )

    def _open_response(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
    ):
        headers = {"User-Agent": USER_AGENT}
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            if hasattr(self.opener, "open"):
                return self.opener.open(request, timeout=self.timeout_sec)
            return self.opener(request, timeout=self.timeout_sec)
        except HTTPError as exc:
            raise RuntimeError(
                f"Chess-Results HTTP error {exc.code}: {exc.reason}"
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError("Chess-Results request timed out") from exc
        except URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise RuntimeError("Chess-Results request timed out") from exc
            raise RuntimeError(f"Chess-Results request failed: {reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"Chess-Results request failed: {exc}") from exc

    @staticmethod
    def _close_response(response: object) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _check_response(response: object) -> None:
        status = _response_status(response)
        if status is not None and status >= 400:
            raise RuntimeError(f"Chess-Results HTTP error {status}")

    def _fetch_page(
        self,
        page_url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
    ) -> _FetchedPage:
        response = self._open_response(page_url, method=method, data=data)
        try:
            self._check_response(response)
            body = response.read()
            final_url = page_url
            geturl = getattr(response, "geturl", None)
            if callable(geturl):
                candidate = geturl()
                if candidate:
                    final_url = str(candidate)
            _same_origin(
                page_url,
                final_url,
                "final response URL",
                allow_approved_shard_redirect=True,
            )
        except RuntimeError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError("Chess-Results page request timed out") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Chess-Results page read failed: {exc}") from exc
        finally:
            self._close_response(response)

        if not isinstance(body, (bytes, bytearray)) or not body:
            raise RuntimeError("Chess-Results page is empty or malformed")
        return _FetchedPage(
            url=final_url,
            html=bytes(body).decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _descriptor_from_page(
        ref: SourceRef,
        page: _FetchedPage,
        parser: _PageParser,
    ) -> SourceDescriptor:
        title = next(
            (candidate for candidate in parser.headings if candidate),
            next((candidate for candidate in parser.page_titles if candidate), None),
        )
        pgn_url = next(
            (
                candidate
                for anchor in parser.anchors
                if (candidate := _pgn_candidate(page.url, anchor)) is not None
            ),
            None,
        )
        event_country, time_control, is_otb = _metadata_hints(parser.visible_text)
        return SourceDescriptor(
            ref=ref,
            title=title,
            pgn_url=pgn_url,
            event_country_hint=event_country,
            time_control_hint=time_control,
            is_otb_hint=is_otb,
        )

    @staticmethod
    def _form_request(
        form: _HtmlForm,
        page_url: str,
        submit: _FormControl,
        overrides: dict[str, str] | None = None,
    ) -> tuple[str, str, bytes | None]:
        submit_attrs = dict(submit.attributes)
        action = urljoin(
            page_url,
            submit_attrs.get("formaction") or form.action or page_url,
        )
        _same_origin(page_url, action, "form action")
        method = (
            submit_attrs.get("formmethod") or form.method or "GET"
        ).upper()
        if method not in {"GET", "POST"}:
            raise RuntimeError(
                f"Chess-Results form method {method!r} is not supported"
            )

        overrides = overrides or {}
        fields: list[tuple[str, str]] = []
        for control in form.controls:
            if not control.successful or control.kind == "submit":
                continue
            fields.append((control.name, overrides.get(control.name, control.value)))
        fields.append((submit.name, overrides.get(submit.name, submit.value)))
        encoded = urlencode(fields).encode("utf-8")
        if method == "GET":
            parsed = urlparse(action)
            query = "&".join(part for part in (parsed.query, encoded.decode()) if part)
            return urlunparse(parsed._replace(query=query)), method, None
        return action, method, encoded

    def _submit_form(
        self,
        form: _HtmlForm,
        page_url: str,
        submit: _FormControl,
        overrides: dict[str, str] | None = None,
    ) -> _FetchedPage:
        url, method, data = self._form_request(form, page_url, submit, overrides)
        return self._fetch_page(url, method=method, data=data)

    def describe(self, ref: SourceRef) -> SourceDescriptor:
        """Fetch tournament metadata; no games navigation is required."""
        ref = self._validate_ref(ref)
        page = self._fetch_page(ref.source_url)
        return self._descriptor_from_page(ref, page, _parse_page(page.html))

    def _download_response(
        self,
        url: str,
        destination: Path,
        *,
        method: str = "GET",
        data: bytes | None = None,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part_path = Path(f"{destination}.part")
        response = None
        self.last_download_filename = None
        self.last_download_content_type = None
        self.last_download_content_disposition = None
        self.last_download_status = None
        self.last_download_byte_count = 0
        self.last_download_looks_like_pgn = False
        try:
            response = self._open_response(url, method=method, data=data)
            self._check_response(response)
            final_url = url
            geturl = getattr(response, "geturl", None)
            if callable(geturl):
                candidate = geturl()
                if candidate:
                    final_url = str(candidate)
            _same_origin(
                url,
                final_url,
                "final response URL",
                allow_approved_shard_redirect=True,
            )
            self.last_download_status = _response_status(response)
            self.last_download_content_type = _content_type(response)
            self.last_download_content_disposition = _response_header(
                response, "Content-Disposition"
            )
            self.last_download_filename = _content_disposition_filename(response)

            prefix = bytearray()
            byte_count = 0
            with part_path.open("wb") as output:
                while True:
                    block = response.read(_DOWNLOAD_CHUNK_SIZE)
                    if not isinstance(block, (bytes, bytearray)):
                        raise RuntimeError(
                            "Chess-Results PGN response returned non-byte data"
                        )
                    if not block:
                        break
                    if len(prefix) < _PGN_PROBE_BYTES:
                        prefix.extend(block[: _PGN_PROBE_BYTES - len(prefix)])
                    output.write(block)
                    byte_count += len(block)
                self.last_download_byte_count = byte_count
                if byte_count == 0:
                    raise RuntimeError("Chess-Results PGN download was empty")
                if (
                    self.last_download_content_type == "text/html"
                    or _looks_like_html(bytes(prefix))
                ):
                    raise RuntimeError(
                        "Chess-Results response was HTML, not a PGN download"
                    )
                self.last_download_looks_like_pgn = _looks_like_pgn(bytes(prefix))
                if not self.last_download_looks_like_pgn:
                    raise RuntimeError(
                        "Chess-Results response did not look like a PGN download"
                    )
                output.flush()
                os.fsync(output.fileno())
            os.replace(part_path, destination)
            return destination
        except RuntimeError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(f"Chess-Results PGN download failed: {exc}") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Chess-Results PGN download failed: {exc}") from exc
        finally:
            if response is not None:
                self._close_response(response)
            try:
                part_path.unlink()
            except FileNotFoundError:
                pass

    def download_pgn(self, ref: SourceRef, destination: Path) -> Path:
        """Search the provider database, then submit its PGN form."""
        ref = self._validate_ref(ref)
        database_key = database_key_from_ref(ref)
        destination = Path(destination).expanduser()

        search_url = self._search_page_url(ref)
        search_page = self._fetch_page(search_url)
        search_parser = _parse_page(search_page.html)
        search_form, key_control, search_submit = _search_form(search_parser)
        result_page = self._submit_form(
            search_form,
            search_page.url,
            search_submit,
            {key_control.name: database_key},
        )
        result_parser = _parse_page(result_page.html)
        rounds = _parse_database_result(result_parser, database_key)
        download_form, download_submit = _download_form(result_parser)
        round_from, round_to = _round_controls(download_form)

        overrides = {
            round_from.name: str(rounds[0][0]),
            round_to.name: str(rounds[-1][0]),
        }
        try:
            result_key_control = _database_key_control(
                download_form,
                allow_hidden=True,
            )
        except RuntimeError:
            result_key_control = None
        if result_key_control is not None:
            overrides[result_key_control.name] = database_key

        url, method, data = self._form_request(
            download_form,
            result_page.url,
            download_submit,
            overrides,
        )
        return self._download_response(
            url,
            destination,
            method=method,
            data=data,
        )


__all__ = [
    "ChessResultsAdapter",
    "PROVIDER",
    "USER_AGENT",
    "database_key_from_ref",
]