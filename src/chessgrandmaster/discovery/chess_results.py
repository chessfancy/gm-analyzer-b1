"""Read-only Chess-Results candidate discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import calendar
import re
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .name_filter import (
    VIETNAMESE_SURNAMES,
    is_confirmed_vie_fide_id,
    vietnamese_name_hint,
)


PROVIDER = "chess-results"
_BASE_URL = "https://chess-results.com"
_FEDERATION_PATH = "/fed.aspx"
_PLAYER_SEARCH_PATH = "/spielersuche.aspx"
_MAX_HTML_BYTES = 4 * 1024 * 1024
_TNR_PATH_RE = re.compile(r"^/tnr(?P<number>\d+)\.aspx$", re.IGNORECASE)
_SUPPORTED_NUMBERED_HOST_RE = re.compile(
    r"^s\d+\.chess-results\.com$", re.IGNORECASE
)


@dataclass(frozen=True)
class PlayerEvidence:
    """One provider-returned player observation attached to a candidate."""

    player_name: str | None
    fide_id: int | None
    source_fed: str | None
    confirmed_vie: bool
    vietnamese_name_hint: str


@dataclass(frozen=True)
class ChessResultsCandidate:
    provider: str
    external_id: str
    source_url: str
    title: str | None
    time_control_hint: str | None
    evidence: tuple[str, ...]
    event_country: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    database_key: str | None = None
    provider_game_count: int | None = None
    player_name: str | None = None
    fide_id: int | None = None
    source_fed: str | None = None
    confirmed_vie: bool = False
    vietnamese_name_hint: str = "none"
    player_evidence: tuple[PlayerEvidence, ...] = ()

    @property
    def priority_tier(self) -> str:
        if self.confirmed_vie or self.vietnamese_name_hint == "strong":
            return "BOOK_HIGH"
        if self.event_country == "VIE":
            return "BOOK_MEDIUM"
        return "GENERAL"

    @property
    def priority_evidence(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.confirmed_vie:
            reasons.append("confirmed_vie_player")
        if self.vietnamese_name_hint == "strong":
            reasons.append("strong_vietnamese_name")
        if self.event_country == "VIE":
            reasons.append("event_country:VIE")
        return tuple(reasons)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "provider": self.provider,
            "external_id": self.external_id,
            "source_url": self.source_url,
            "title": self.title,
            "time_control_hint": self.time_control_hint,
            "event_country": self.event_country,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "database_key": self.database_key,
            "provider_game_count": self.provider_game_count,
            "priority_tier": self.priority_tier,
            "priority_evidence": list(self.priority_evidence),
            "evidence": list(self.evidence),
            "player_name": self.player_name,
            "fide_id": self.fide_id,
            "source_fed": self.source_fed,
            "confirmed_vie": self.confirmed_vie,
            "vietnamese_name_hint": self.vietnamese_name_hint,
            "player_evidence": [
                {
                    "player_name": item.player_name,
                    "fide_id": item.fide_id,
                    "source_fed": item.source_fed,
                    "confirmed_vie": item.confirmed_vie,
                    "vietnamese_name_hint": item.vietnamese_name_hint,
                }
                for item in self.player_evidence
            ],
        }


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple[ChessResultsCandidate, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorpusWindow:
    """One requested or recursively split Tournament Database interval."""

    from_date: str
    to_date: str
    returned_row_count: int
    split: bool = False
    saturated: bool = False
    parsed_candidate_count: int = 0

    @property
    def source_window_saturated(self) -> bool:
        return self.saturated

    def to_dict(self) -> dict[str, object]:
        return {
            "from_date": self.from_date,
            "to_date": self.to_date,
            "returned_row_count": self.returned_row_count,
            "parsed_candidate_count": self.parsed_candidate_count,
            "split": self.split,
            "saturated": self.saturated,
            "source_window_saturated": self.source_window_saturated,
        }


@dataclass(frozen=True)
class CorpusDiscoveryResult:
    """Read-only full-year corpus scan result and window audit trail."""

    candidates: tuple[ChessResultsCandidate, ...]
    windows: tuple[CorpusWindow, ...]
    errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether every requested source window completed without saturation."""
        return not self.errors and not self.saturated_windows

    @property
    def saturated_windows(self) -> tuple[CorpusWindow, ...]:
        return tuple(window for window in self.windows if window.saturated)

    @property
    def priority_counts(self) -> dict[str, int]:
        counts = {"BOOK_HIGH": 0, "BOOK_MEDIUM": 0, "GENERAL": 0}
        for candidate in self.candidates:
            counts[candidate.priority_tier] += 1
        return counts

    def to_dict(self) -> dict[str, object]:
        return {
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "windows": [window.to_dict() for window in self.windows],
            "errors": list(self.errors),
            "candidate_count": len(self.candidates),
            "priority_counts": self.priority_counts,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class _Link:
    href: str
    text: str
    context: str = ""


@dataclass(frozen=True)
class _TableRow:
    cells: tuple[str, ...]
    links: tuple[_Link, ...]
    table_id: int = 0
    header: bool = False


@dataclass(frozen=True)
class _FormControl:
    name: str
    value: str
    kind: str
    input_type: str
    attributes: tuple[tuple[str, str], ...]
    label: str = ""
    options: tuple[tuple[str, str], ...] = ()
    successful: bool = True

    @property
    def semantic_text(self) -> str:
        attrs = dict(self.attributes)
        return " ".join(
            (
                self.name,
                self.label,
                attrs.get("id", ""),
                attrs.get("class", ""),
                attrs.get("title", ""),
                attrs.get("aria-label", ""),
                attrs.get("placeholder", ""),
                " ".join(text for _, text in self.options),
                self.value if self.kind == "submit" else "",
            )
        ).casefold()


@dataclass(frozen=True)
class _HtmlForm:
    action: str | None
    method: str
    controls: tuple[_FormControl, ...]


@dataclass
class _FormBuilder:
    action: str | None
    method: str
    controls: list[_FormControl] = field(default_factory=list)


@dataclass
class _OpenButton:
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
    options: list[_OpenOption] = field(default_factory=list)
    option: _OpenOption | None = None


@dataclass
class _OpenCapture:
    tag: str
    key: str
    parts: list[str] = field(default_factory=list)


class _DiscoveryParser(HTMLParser):
    """Parse public discovery forms and result rows without domain state."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_HtmlForm] = []
        self.rows: list[_TableRow] = []
        self.anchors: list[_Link] = []
        self.relation_options: list[_Link] = []
        self.labels: dict[str, str] = {}
        self._form: _FormBuilder | None = None
        self._button_stack: list[_OpenButton] = []
        self._select_stack: list[_OpenSelect] = []
        self._capture_stack: list[_OpenCapture] = []
        self._row_cells: list[str] | None = None
        self._row_links: list[_Link] = []
        self._row_table_id = 0
        self._row_header = False
        self._cell_parts: list[str] | None = None
        self._anchor: tuple[str, list[str], str] | None = None
        self._context_stack: list[tuple[str, str]] = []
        self._ignored_depth = 0
        self._table_stack: list[int] = []
        self._next_table_id = 0

    @staticmethod
    def _text(parts: list[str]) -> str:
        return " ".join(" ".join(parts).split())

    @staticmethod
    def _attrs_tuple(
        attrs: list[tuple[str, str | None]],
    ) -> tuple[tuple[str, str], ...]:
        return tuple((key.casefold(), value or "") for key, value in attrs)

    @staticmethod
    def _label_keys(value: str) -> tuple[str, ...]:
        normalized = value.casefold()
        keys: list[str] = []

        def add(key: str) -> None:
            if key and key not in keys:
                keys.append(key)

        add(normalized)
        if "lb_" in normalized:
            add(normalized[normalized.find("lb_") :])
        if "_" in normalized:
            add(normalized.rsplit("_", 1)[-1])
        return tuple(keys)

    def _store_label(self, key: str, text: str) -> None:
        if not key or not text:
            return
        for label_key in self._label_keys(key):
            self.labels[label_key] = text

    def _control_label(self, attrs: tuple[tuple[str, str], ...]) -> str:
        attr_map = dict(attrs)
        candidates: list[str] = []
        if attr_map.get("id"):
            candidates.extend(self._label_keys(attr_map["id"]))
        for token in attr_map.get("aria-labelledby", "").split():
            candidates.extend(self._label_keys(token))
        for key in candidates:
            if key in self.labels:
                return self.labels[key]
        return ""

    def _finish_button(self) -> None:
        if self._form is None or not self._button_stack:
            return
        button = self._button_stack.pop()
        if not button.name or button.input_type in {"reset", "button"}:
            return
        text = self._text(button.parts)
        self._form.controls.append(
            _FormControl(
                name=button.name,
                value=button.value or text,
                kind="submit",
                input_type=button.input_type,
                attributes=button.attributes,
                label=text,
            )
        )

    def _finish_option(self) -> None:
        if not self._select_stack or self._select_stack[-1].option is None:
            return
        option = self._select_stack[-1].option
        option.value = option.value or self._text(option.parts)
        self._select_stack[-1].options.append(option)
        self._select_stack[-1].option = None

    def _finish_select(self) -> None:
        if not self._select_stack:
            return
        self._finish_option()
        select = self._select_stack.pop()
        chosen = next((item for item in select.options if item.selected), None)
        chosen = chosen or (select.options[0] if select.options else None)
        attrs = dict(select.attributes)
        if select.name:
            context = " ".join(
                value
                for key, value in select.attributes
                if key in {"id", "name", "class", "aria-label", "title"}
            )
            context = " ".join(
                [
                    context,
                    " ".join(value for _, value in self._context_stack if value),
                ]
            ).strip()
            for option in select.options:
                if option.value:
                    self.relation_options.append(
                        _Link(option.value, self._text(option.parts), context)
                    )
            if self._form is not None:
                self._form.controls.append(
                    _FormControl(
                        name=select.name,
                        value=chosen.value if chosen is not None else "",
                        kind="select",
                        input_type="select",
                        attributes=select.attributes,
                        label=self._control_label(select.attributes),
                        options=tuple(
                            (option.value, self._text(option.parts))
                            for option in select.options
                            if option.value
                        ),
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
        controls = tuple(
            _FormControl(
                name=control.name,
                value=control.value,
                kind=control.kind,
                input_type=control.input_type,
                attributes=control.attributes,
                label=control.label or self._control_label(control.attributes),
                options=control.options,
                successful=control.successful,
            )
            for control in self._form.controls
        )
        self.forms.append(_HtmlForm(self._form.action, self._form.method, controls))
        self._form = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        attr_map = {key.casefold(): value or "" for key, value in attrs}
        attributes = self._attrs_tuple(attrs)
        if tag not in {"input", "br", "img", "meta", "link", "hr"}:
            context = " ".join(
                value
                for key in ("id", "name", "class", "aria-label", "title")
                if (value := attr_map.get(key, ""))
            )
            self._context_stack.append((tag, context))
        if tag in {"label", "span", "div"}:
            key = attr_map.get("for", "") or attr_map.get("id", "")
            classes = attr_map.get("class", "").casefold()
            if tag == "label" or "label" in classes or "lb_" in key.casefold():
                self._capture_stack.append(_OpenCapture(tag, key))
        if tag == "table":
            self._next_table_id += 1
            self._table_stack.append(self._next_table_id)
        if tag == "form":
            self._finish_form()
            self._form = _FormBuilder(
                action=attr_map.get("action"),
                method=attr_map.get("method", "GET").upper(),
            )
        elif tag == "input" and self._form is not None:
            name = attr_map.get("name", "")
            input_type = attr_map.get("type", "text").casefold()
            if name and input_type not in {"reset", "button"}:
                successful = "disabled" not in attr_map
                if input_type in {"checkbox", "radio"}:
                    successful = successful and "checked" in attr_map
                self._form.controls.append(
                    _FormControl(
                        name=name,
                        value=attr_map.get("value", "on" if input_type == "checkbox" else ""),
                        kind="submit" if input_type in {"submit", "image"} else "input",
                        input_type=input_type,
                        attributes=attributes,
                        successful=successful,
                    )
                )
        elif tag == "button" and self._form is not None:
            self._button_stack.append(
                _OpenButton(
                    name=attr_map.get("name", ""),
                    value=attr_map.get("value", ""),
                    input_type=attr_map.get("type", "submit").casefold(),
                    attributes=attributes,
                )
            )
        elif tag == "select":
            self._select_stack.append(
                _OpenSelect(attr_map.get("name", ""), attributes)
            )
        elif tag == "option" and self._select_stack:
            self._finish_option()
            self._select_stack[-1].option = _OpenOption(
                value=attr_map.get("value", ""),
                selected="selected" in attr_map,
            )
        if tag == "tr":
            self._row_cells = []
            self._row_links = []
            self._row_table_id = self._table_stack[-1] if self._table_stack else 0
            self._row_header = False
        elif tag in {"td", "th"} and self._row_cells is not None:
            if tag == "th":
                self._row_header = True
            self._cell_parts = []
        elif tag == "a":
            href = attr_map.get("href", "")
            if href:
                context = " ".join(value for _, value in self._context_stack if value)
                self._anchor = (href, [], context)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "a" and self._anchor is not None:
            href, parts, context = self._anchor
            link = _Link(href, self._text(parts), context)
            self.anchors.append(link)
            if self._row_cells is not None:
                self._row_links.append(link)
            self._anchor = None
        elif tag == "button":
            self._finish_button()
        elif tag == "option":
            self._finish_option()
        elif tag == "select":
            self._finish_select()
        elif tag in {"td", "th"} and self._row_cells is not None:
            self._row_cells.append(self._text(self._cell_parts or []))
            self._cell_parts = None
        elif tag == "tr" and self._row_cells is not None:
            self.rows.append(
                _TableRow(
                    tuple(self._row_cells),
                    tuple(self._row_links),
                    table_id=self._row_table_id,
                    header=self._row_header,
                )
            )
            self._row_cells = None
            self._row_links = []
        elif tag == "form":
            self._finish_form()
        elif tag == "table" and self._table_stack:
            self._table_stack.pop()
        for index in range(len(self._capture_stack) - 1, -1, -1):
            capture = self._capture_stack[index]
            if capture.tag != tag:
                continue
            self._capture_stack.pop(index)
            self._store_label(capture.key, self._text(capture.parts))
            break
        for index in range(len(self._context_stack) - 1, -1, -1):
            if self._context_stack[index][0] == tag:
                self._context_stack.pop(index)
                break

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._cell_parts is not None:
            self._cell_parts.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)
        if self._button_stack:
            self._button_stack[-1].parts.append(data)
        if self._select_stack and self._select_stack[-1].option is not None:
            self._select_stack[-1].option.parts.append(data)
        for capture in self._capture_stack:
            capture.parts.append(data)

    def finish(self) -> None:
        self._finish_form()
        if self._row_cells is not None:
            if self._cell_parts is not None:
                self._row_cells.append(self._text(self._cell_parts))
            self.rows.append(
                _TableRow(
                    tuple(self._row_cells),
                    tuple(self._row_links),
                    table_id=self._row_table_id,
                    header=self._row_header,
                )
            )
            self._row_cells = None
            self._row_links = []
        if self._anchor is not None:
            href, parts, context = self._anchor
            self.anchors.append(_Link(href, self._text(parts)))
            self._anchor = None
        for capture in self._capture_stack:
            self._store_label(capture.key, self._text(capture.parts))
        self._capture_stack.clear()


def _parse_discovery_page(body: bytes) -> _DiscoveryParser:
    if not body:
        raise RuntimeError("Chess-Results page is empty")
    parser = _DiscoveryParser()
    try:
        parser.feed(body.decode("utf-8", errors="replace"))
        parser.close()
        parser.finish()
    except Exception as exc:
        raise RuntimeError("Chess-Results page is malformed") from exc
    if not parser.forms and not parser.rows and not parser.anchors and not parser.relation_options:
        raise RuntimeError("Chess-Results page is malformed")
    return parser


def _control_words(control: _FormControl) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", control.semantic_text))


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _find_player_form(
    parser: _DiscoveryParser,
) -> tuple[_HtmlForm, _FormControl, _FormControl]:
    for form in parser.forms:
        fide_controls = [
            control
            for control in form.controls
            if control.kind == "input"
            and control.input_type in {"text", "search"}
            and (
                "fideid" in re.sub(r"[^a-z0-9]", "", control.semantic_text)
                or {"fide", "id"} <= _control_words(control)
            )
        ]
        submits = [
            control
            for control in form.controls
            if control.kind == "submit"
            and _control_words(control) & {"search", "find", "query"}
            and not (_control_words(control) & {"download", "excel", "export"})
        ]
        if len(fide_controls) == 1 and len(submits) == 1:
            return form, fide_controls[0], submits[0]
    raise RuntimeError("Chess-Results Player Database exposes no usable search form")


def _source_fed_control(form: _HtmlForm) -> _FormControl:
    candidates = []
    for control in form.controls:
        if control.input_type in {"checkbox", "radio", "hidden"}:
            continue
        words = _control_words(control)
        compact = re.sub(r"[^a-z0-9]", "", control.name.casefold())
        if "tournament" in words and "country" in words:
            continue
        if ({"country", "fide", "abbreviation"} <= words) or compact.endswith(
            "txtfed"
        ):
            candidates.append(control)
    if len(candidates) != 1:
        raise RuntimeError(
            "Chess-Results Player Database exposes no unique source-FED field"
        )
    return candidates[0]


def _overseas_control(form: _HtmlForm) -> _FormControl:
    candidates = [
        control
        for control in form.controls
        if control.input_type == "checkbox"
        and (
            "overseas" in _control_words(control)
            or "ausland" in re.sub(r"[^a-z0-9]", "", control.name.casefold())
            or "foreign" in _control_words(control)
        )
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            "Chess-Results Player Database exposes no unique overseas field"
        )
    return candidates[0]


def _surname_control(form: _HtmlForm) -> _FormControl:
    candidates = []
    for control in form.controls:
        if control.input_type in {"checkbox", "radio", "hidden"}:
            continue
        words = _control_words(control)
        compact = re.sub(r"[^a-z0-9]", "", control.name.casefold())
        if "fide" in words or "id" in words:
            continue
        if {"last", "name"} <= words or compact.endswith("txtnachname"):
            candidates.append(control)
    if len(candidates) != 1:
        raise RuntimeError(
            "Chess-Results Player Database exposes no unique surname field"
        )
    return candidates[0]


def _form_request(
    form: _HtmlForm,
    page_url: str,
    submit: _FormControl,
    overrides: dict[str, str],
) -> tuple[str, str, bytes | None]:
    action = urljoin(page_url, form.action or page_url)
    _same_origin(page_url, action)
    method = form.method.upper() or "GET"
    if method not in {"GET", "POST"}:
        raise RuntimeError(f"Chess-Results form method {method!r} is not supported")
    fields: list[tuple[str, str]] = []
    for control in form.controls:
        if control.kind == "submit":
            continue
        if control.name in overrides:
            fields.append((control.name, overrides[control.name]))
        elif control.successful:
            fields.append((control.name, control.value))
    fields.append((submit.name, overrides.get(submit.name, submit.value)))
    encoded = urlencode(fields).encode("utf-8")
    if method == "GET":
        parsed = urlparse(action)
        query = "&".join(part for part in (parsed.query, encoded.decode()) if part)
        return urlunparse(parsed._replace(query=query)), method, None
    return action, method, encoded


def _header_indices(row: _TableRow) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, cell in enumerate(row.cells):
        compact = re.sub(r"[^a-z0-9]", "", cell.casefold())
        if compact in {"name", "playername"}:
            result.setdefault("name", index)
        elif compact in {"fideid", "fidenumber"}:
            result.setdefault("fide_id", index)
        elif compact in {"fed", "federation", "country"}:
            result.setdefault("fed", index)
        elif "tournament" in compact:
            result.setdefault("tournament", index)
        elif compact in {"enddate", "date", "tournamentenddate"}:
            result.setdefault("date", index)
    return result


def _tournament_link(page_url: str, links: tuple[_Link, ...]) -> tuple[str, str] | None:
    candidates = []
    for link in links:
        parsed = _tnr_from_href(page_url, link.href)
        if parsed is None:
            continue
        query = urlparse(parsed[1]).query.casefold()
        candidates.append(("art=" in query or "snr=" in query, parsed, link))
    if not candidates:
        return None
    _, parsed, _ = sorted(candidates, key=lambda item: item[0])[0]
    return parsed


def _player_candidates(
    page_url: str,
    parser: _DiscoveryParser,
    evidence: tuple[str, ...],
    *,
    strong_only: bool = False,
    surname_seed: str | None = None,
) -> list[ChessResultsCandidate]:
    candidates: list[ChessResultsCandidate] = []
    header_index = next(
        (index for index, row in enumerate(parser.rows) if "name" in _header_indices(row)),
        None,
    )
    if header_index is None:
        return candidates
    indices = _header_indices(parser.rows[header_index])
    required = {"name", "fide_id", "fed", "tournament"}
    if not required <= indices.keys():
        return candidates
    for row in parser.rows[header_index + 1 :]:
        link = _tournament_link(page_url, row.links)
        if link is None:
            continue
        external_id, source_url = link
        name = row.cells[indices["name"]].strip() or None
        raw_fide_id = row.cells[indices["fide_id"]].strip()
        fide_id = int(raw_fide_id) if raw_fide_id.isdigit() else None
        source_fed = row.cells[indices["fed"]].strip().upper() or None
        hint = vietnamese_name_hint(name or "")
        if strong_only and hint.strength != "strong":
            continue
        player_evidence = PlayerEvidence(
            player_name=name,
            fide_id=fide_id,
            source_fed=source_fed,
            confirmed_vie=source_fed == "VIE"
            or (fide_id is not None and is_confirmed_vie_fide_id(fide_id)),
            vietnamese_name_hint=hint.strength,
        )
        reasons = set(evidence)
        if fide_id is not None and is_confirmed_vie_fide_id(fide_id):
            reasons.add("confirmed_vie:fide_id_set")
        if surname_seed is not None:
            reasons.update(
                {
                    f"name_hint:{hint.strength}",
                    f"surname_seed:{surname_seed}",
                    f"source_fed:{source_fed or 'unknown'}",
                }
            )
        candidates.append(
            ChessResultsCandidate(
                provider=PROVIDER,
                external_id=external_id,
                source_url=source_url,
                title=row.cells[indices["tournament"]].strip() or None,
                time_control_hint=_time_control(
                    row.cells[indices["tournament"]]
                ),
                evidence=tuple(sorted(reasons)),
                player_name=name,
                fide_id=fide_id,
                source_fed=source_fed,
                confirmed_vie=player_evidence.confirmed_vie,
                vietnamese_name_hint=hint.strength,
                player_evidence=(player_evidence,),
            )
        )
    return candidates



def _supported_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.casefold().rstrip(".")
    return normalized in {"chess-results.com", "www.chess-results.com"} or bool(
        _SUPPORTED_NUMBERED_HOST_RE.fullmatch(normalized)
    )


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not _supported_host(
        parsed.hostname
    ):
        raise RuntimeError("Chess-Results URL is outside approved source hosts")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("Chess-Results URL contains credentials")
    port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    return parsed.scheme.casefold(), parsed.hostname.casefold().rstrip("."), port


def _same_origin(base_url: str, target_url: str) -> None:
    if _origin(base_url) != _origin(target_url):
        raise RuntimeError("Chess-Results navigation is cross-origin")


def _time_control(text: str) -> str | None:
    if re.search(r"(?:^|[^a-z])st(?:[^a-z]|$)", text, re.IGNORECASE):
        return "standard"
    if re.search(r"(?:^|[^a-z])rp(?:[^a-z]|$)", text, re.IGNORECASE):
        return "rapid"
    if re.search(r"(?:^|[^a-z])bz(?:[^a-z]|$)", text, re.IGNORECASE):
        return "blitz"
    lowered = text.casefold()
    for marker, value in (
        ("standard", "standard"),
        ("classical", "standard"),
        ("rapid", "rapid"),
        ("blitz", "blitz"),
    ):
        if marker in lowered:
            return value
    return None


def _tnr_from_href(page_url: str, href: str) -> tuple[str, str] | None:
    candidate = urljoin(page_url, href)
    try:
        _origin(candidate)
    except (RuntimeError, ValueError):
        return None
    parsed = urlparse(candidate)
    match = _TNR_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return f"tnr{match.group('number')}", candidate


def _candidate_sort_key(candidate: ChessResultsCandidate) -> tuple[object, ...]:
    """Sort priority tiers, time control, then stable identity."""
    evidence_rank = {
        "BOOK_HIGH": 0,
        "BOOK_MEDIUM": 1,
        "GENERAL": 2,
    }[candidate.priority_tier]
    signal_rank = (
        0
        if candidate.confirmed_vie
        else 1
        if candidate.vietnamese_name_hint == "strong"
        else 2
    )
    time_rank = {"standard": 0, "rapid": 1, "blitz": 2}.get(
        candidate.time_control_hint, 3
    )
    return (
        evidence_rank,
        signal_rank,
        time_rank,
        candidate.external_id,
        candidate.title or "",
    )


def _ordered_unique(
    candidates: list[ChessResultsCandidate], limit: int | None = None
) -> tuple[ChessResultsCandidate, ...]:
    merged: dict[tuple[str, str], ChessResultsCandidate] = {}
    for candidate in candidates:
        key = (candidate.provider, candidate.external_id)
        if key not in merged:
            merged[key] = candidate
            continue
        previous = merged[key]
        merged_player_evidence: list[PlayerEvidence] = []
        for item in (*previous.player_evidence, *candidate.player_evidence):
            if item not in merged_player_evidence:
                merged_player_evidence.append(item)
        player_evidence = tuple(merged_player_evidence)
        primary_player = next(
            iter(
                sorted(
                    enumerate(player_evidence),
                    key=lambda pair: (
                        not pair[1].confirmed_vie,
                        pair[1].vietnamese_name_hint != "strong",
                        pair[0],
                    ),
                )
            ),
            (0, None),
        )[1]
        hint = previous.vietnamese_name_hint
        if hint != "strong" and candidate.vietnamese_name_hint == "strong":
            hint = "strong"
        elif hint == "none" and candidate.vietnamese_name_hint != "none":
            hint = candidate.vietnamese_name_hint
        merged[key] = ChessResultsCandidate(
            provider=previous.provider,
            external_id=previous.external_id,
            source_url=min(previous.source_url, candidate.source_url),
            title=min(
                (value for value in (previous.title, candidate.title) if value),
                default=None,
            ),
            time_control_hint=previous.time_control_hint or candidate.time_control_hint,
            event_country=previous.event_country or candidate.event_country,
            from_date=previous.from_date or candidate.from_date,
            to_date=previous.to_date or candidate.to_date,
            database_key=previous.database_key or candidate.database_key,
            provider_game_count=max(
                value
                for value in (
                    previous.provider_game_count,
                    candidate.provider_game_count,
                )
                if value is not None
            )
            if any(
                value is not None
                for value in (
                    previous.provider_game_count,
                    candidate.provider_game_count,
                )
            )
            else None,
            evidence=tuple(sorted(set(previous.evidence) | set(candidate.evidence))),
            player_name=(
                primary_player.player_name
                if primary_player is not None
                else previous.player_name or candidate.player_name
            ),
            fide_id=(
                primary_player.fide_id
                if primary_player is not None
                else previous.fide_id or candidate.fide_id
            ),
            source_fed=(
                primary_player.source_fed
                if primary_player is not None
                else previous.source_fed or candidate.source_fed
            ),
            confirmed_vie=(
                previous.confirmed_vie
                or candidate.confirmed_vie
                or any(item.confirmed_vie for item in player_evidence)
            ),
            vietnamese_name_hint=hint,
            player_evidence=player_evidence,
        )
    ordered = sorted(merged.values(), key=_candidate_sort_key)
    return tuple(ordered if limit is None else ordered[:limit])


def merge_corpus_priority(
    corpus_candidates: list[ChessResultsCandidate] | tuple[ChessResultsCandidate, ...],
    priority_results: list[DiscoveryResult] | tuple[DiscoveryResult, ...],
    *,
    limit: int | None = None,
) -> tuple[ChessResultsCandidate, ...]:
    """Merge priority evidence by exact provider identity, never by title."""
    corpus = _ordered_unique(list(corpus_candidates))
    corpus_keys = {(item.provider, item.external_id) for item in corpus}
    enrichment: list[ChessResultsCandidate] = []
    for result in priority_results:
        for candidate in result.candidates:
            if (candidate.provider, candidate.external_id) in corpus_keys:
                enrichment.append(candidate)
    return _ordered_unique([*corpus, *enrichment], limit=limit)


def _corpus_control_words(control: _FormControl) -> set[str]:
    return _control_words(control)


def _corpus_option_value(
    control: _FormControl,
    predicate: Any,
) -> str | None:
    for value, text in control.options:
        if predicate(text.casefold()):
            return value
    return None


def _corpus_control(
    form: _HtmlForm,
    predicate: Any,
    description: str,
) -> _FormControl:
    candidates = [control for control in form.controls if predicate(control)]
    if len(candidates) != 1:
        raise RuntimeError(
            f"Chess-Results Tournament Database exposes no unique {description} control"
        )
    return candidates[0]


def _corpus_form(
    parser: _DiscoveryParser,
    *,
    max_lines: int,
    country: str | None,
) -> tuple[_HtmlForm, dict[str, _FormControl], dict[str, str]]:
    """Find Tournament Database controls from labels, IDs, and option text."""
    for form in parser.forms:
        try:
            from_control = _corpus_control(
                form,
                lambda control: control.input_type in {"date", "text"}
                and (
                    "from" in _corpus_control_words(control)
                    or "start" in _corpus_control_words(control)
                    or "von" in _compact(control.semantic_text)
                ),
                "tournament-end-from",
            )
            to_control = _corpus_control(
                form,
                lambda control: control.input_type in {"date", "text"}
                and (
                    "to" in _corpus_control_words(control)
                    or "bis" in _compact(control.semantic_text)
                ),
                "tournament-end-to",
            )
            time_control = _corpus_control(
                form,
                lambda control: control.kind == "select"
                and (
                    {
                        "time",
                        "control",
                    }
                    <= _corpus_control_words(control)
                    or "bedenkzeit" in _compact(control.semantic_text)
                    or _corpus_option_value(
                        control,
                        lambda text: "standard" in text or "classical" in text,
                    )
                    is not None
                ),
                "time-control",
            )
            finished = _corpus_control(
                form,
                lambda control: control.input_type == "checkbox"
                and (
                    "finished" in _corpus_control_words(control)
                    or "complete" in _corpus_control_words(control)
                    or "zuende" in _compact(control.semantic_text)
                ),
                "only-finished",
            )
            games_available = _corpus_control(
                form,
                lambda control: control.input_type == "checkbox"
                and (
                    "games" in _corpus_control_words(control)
                    or "available" in _corpus_control_words(control)
                    or "partien" in _compact(control.semantic_text)
                ),
                "games-available",
            )
            max_control = _corpus_control(
                form,
                lambda control: control.kind == "select"
                and (
                    "maximum" in _corpus_control_words(control)
                    or "lines" in _corpus_control_words(control)
                    or "rows" in _corpus_control_words(control)
                    or "anzahl" in _compact(control.semantic_text)
                    or _corpus_option_value(
                        control,
                        lambda text: text.strip() == str(max_lines),
                    )
                    is not None
                ),
                "maximum-lines",
            )
            sort_control = _corpus_control(
                form,
                lambda control: control.kind == "select"
                and (
                    "sort" in _corpus_control_words(control)
                    or _corpus_option_value(
                        control,
                        lambda text: "last update" in text,
                    )
                    is not None
                ),
                "sort",
            )
            country_control = _corpus_control(
                form,
                lambda control: control.kind == "select"
                and (
                    "country" in _corpus_control_words(control)
                    or "federation" in _corpus_control_words(control)
                    or "land" in _compact(control.semantic_text)
                    or _corpus_option_value(
                        control,
                        lambda text: "all countries" in text,
                    )
                ),
                "country",
            )
            submit = _corpus_control(
                form,
                lambda control: control.kind == "submit"
                and not (
                    {"download", "excel", "export"}
                    & _corpus_control_words(control)
                )
                and (
                    {"search", "suchen", "find", "query"}
                    & _corpus_control_words(control)
                ),
                "search-submit",
            )
            database_key = _corpus_control(
                form,
                lambda control: control.input_type in {"text", "search"}
                and (
                    "database" in _corpus_control_words(control)
                    or "dbkey" in _compact(control.semantic_text)
                    or "tnr" in _compact(control.semantic_text)
                ),
                "database-key",
            )
            fide_event_id = _corpus_control(
                form,
                lambda control: control.input_type in {"text", "search"}
                and (
                    "eventid" in _compact(control.semantic_text)
                    or (
                        "event" in _corpus_control_words(control)
                        and "fide" in _corpus_control_words(control)
                    )
                ),
                "fide-event-id",
            )
            tournament = _corpus_control(
                form,
                lambda control: control.input_type in {"text", "search"}
                and (
                    (
                        "tournament" in _corpus_control_words(control)
                        and not (
                            {"director", "organizer", "organiser"}
                            & _corpus_control_words(control)
                        )
                    )
                    or "turnier" in _corpus_control_words(control)
                    or "bez" in _compact(control.semantic_text)
                ),
                "tournament",
            )
        except RuntimeError:
            continue

        standard_value = _corpus_option_value(
            time_control,
            lambda text: "standard" in text or "classical" in text,
        )
        max_value = _corpus_option_value(
            max_control,
            lambda text: text.strip() == str(max_lines),
        )
        sort_value = _corpus_option_value(
            sort_control,
            lambda text: "last update" in text,
        )
        if standard_value is None or max_value is None or sort_value is None:
            continue
        if country is None:
            country_value = _corpus_option_value(
                country_control,
                lambda text: "all countries" in text
                or text.strip() in {"-", "all"},
            )
        else:
            country_upper = country.upper()
            country_value = _corpus_option_value(
                country_control,
                lambda text: text.strip().upper() == country_upper,
            )
            if country_value is None:
                country_value = next(
                    (
                        value
                        for value, text in country_control.options
                        if value.upper() == country_upper
                    ),
                    None,
                )
        if country_value is None:
            continue
        return (
            form,
            {
                "from": from_control,
                "to": to_control,
                "time_control": time_control,
                "finished": finished,
                "games_available": games_available,
                "max_lines": max_control,
                "sort": sort_control,
                "country": country_control,
                "submit": submit,
                "database_key": database_key,
                "fide_event_id": fide_event_id,
                "tournament": tournament,
            },
            {
                "time_control": standard_value,
                "finished": finished.value or "on",
                "games_available": games_available.value or "on",
                "max_lines": max_value,
                "sort": sort_value,
                "country": country_value,
            },
        )
    raise RuntimeError(
        "Chess-Results Tournament Database exposes no usable semantic search form"
    )


def _provider_date(value: str) -> str | None:
    normalized = value.strip()
    for pattern, order in (
        (r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})", "ymd"),
        (r"(\d{2})[./-](\d{2})[./-](\d{4})", "dmy"),
    ):
        match = re.fullmatch(rf"\s*{pattern}\s*", normalized)
        if match is None:
            continue
        parts = [int(match.group(index)) for index in range(1, 4)]
        if order == "dmy":
            parts = [parts[2], parts[1], parts[0]]
        try:
            return date(*parts).isoformat()
        except ValueError:
            return None
    return None


def _corpus_header_indices(row: _TableRow) -> dict[str, int]:
    indices: dict[str, int] = {}
    for index, cell in enumerate(row.cells):
        compact = _compact(cell)
        if compact in {"tournament", "turnier"} or "tournamentname" in compact:
            indices.setdefault("title", index)
        elif compact in {"fed", "federation", "country"}:
            indices.setdefault("event_country", index)
        elif compact in {
            "from",
            "start",
            "startdate",
            "von",
            "begin",
            "beginn",
        }:
            indices.setdefault("from_date", index)
        elif compact in {"to", "end", "enddate", "bis", "ende"}:
            indices.setdefault("to_date", index)
        elif "timecontrol" in compact or compact in {"bedenkzeit", "tempo"}:
            indices.setdefault("time_control", index)
        elif compact in {"n", "games", "gamecount", "partien"}:
            indices.setdefault("game_count", index)
        elif compact in {
            "dbkey",
            "databasekey",
            "tournamentkey",
            "tnr",
            "tnrnr",
            "turniernr",
            "turniernummer",
        }:
            indices.setdefault("database_key", index)
        elif compact in {"eventid", "fideeventid"}:
            indices.setdefault("event_id", index)
    return indices


def _corpus_time_control(value: str) -> str | None:
    lowered = value.casefold()
    if re.search(r"\b(?:rapid|blitz|bullet|lightning)\b", lowered):
        return None
    return "standard"


def _corpus_candidates(
    page_url: str,
    parser: _DiscoveryParser,
    *,
    year: int,
    window_from: str,
    window_to: str,
) -> tuple[list[ChessResultsCandidate], int]:
    header_index = next(
        (
            index
            for index, row in enumerate(parser.rows)
            if {
                "title",
                "event_country",
                "from_date",
                "to_date",
                "database_key",
            }
            <= _corpus_header_indices(row).keys()
        ),
        None,
    )
    if header_index is None:
        page_text = " ".join(
            cell for row in parser.rows for cell in row.cells
        ).casefold()
        if "no tournament was found with this selection" in page_text:
            return [], 0
        raise RuntimeError(
            "Chess-Results Tournament Database result table is missing"
        )
    indices = _corpus_header_indices(parser.rows[header_index])
    header_row = parser.rows[header_index]
    data_rows = [
        row
        for row in parser.rows[header_index + 1 :]
        if row.table_id == header_row.table_id and not row.header
    ]
    provider_result_row_count = len(data_rows)
    candidates: list[ChessResultsCandidate] = []
    for row in data_rows:
        required_indices = [
            indices["title"],
            indices["event_country"],
            indices["from_date"],
            indices["to_date"],
            indices["database_key"],
        ]
        if len(row.cells) <= max(required_indices):
            continue
        key = row.cells[indices["database_key"]].strip()
        if not key.isdigit():
            continue
        from_value = _provider_date(row.cells[indices["from_date"]])
        to_value = _provider_date(row.cells[indices["to_date"]])
        if from_value is None or to_value is None or not to_value.startswith(f"{year:04d}-"):
            continue
        if "game_count" in indices and len(row.cells) > indices["game_count"]:
            raw_count = re.sub(r"[^0-9]", "", row.cells[indices["game_count"]])
            game_count = int(raw_count) if raw_count else None
            if game_count == 0:
                continue
        else:
            game_count = None
        raw_time_control = (
            row.cells[indices["time_control"]]
            if "time_control" in indices and len(row.cells) > indices["time_control"]
            else ""
        )
        time_control = _corpus_time_control(raw_time_control)
        if time_control is None:
            continue
        event_country = row.cells[indices["event_country"]].strip().upper() or None
        title = row.cells[indices["title"]].strip() or None
        source_url = urljoin(page_url, f"/tnr{key}.aspx")
        for link in row.links:
            parsed = _tnr_from_href(page_url, link.href)
            if parsed is not None and parsed[0].casefold() == f"tnr{key}".casefold():
                source_url = parsed[1]
                if link.text:
                    title = link.text
                break
        candidates.append(
            ChessResultsCandidate(
                provider=PROVIDER,
                external_id=f"tnr{key}",
                source_url=source_url,
                title=title,
                time_control_hint=time_control,
                evidence=(
                    f"corpus:year:{year}",
                    f"source_window:{window_from}:{window_to}",
                    "filter:standard",
                    "filter:finished",
                    "filter:games_available",
                ),
                event_country=event_country,
                from_date=from_value,
                to_date=to_value,
                database_key=key,
                provider_game_count=game_count,
            )
        )
    return candidates, provider_result_row_count


def _calendar_windows(year: int) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            date(year, month, 1).isoformat(),
            date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
        )
        for month in range(1, 13)
    )


def _split_date_window(from_date: str, to_date: str) -> tuple[tuple[str, str], tuple[str, str]]:
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    midpoint = start + timedelta(days=(end - start).days // 2)
    return (start.isoformat(), midpoint.isoformat()), (
        (midpoint + timedelta(days=1)).isoformat(),
        end.isoformat(),
    )


def _family_relation_allowed(link: _Link) -> bool:
    context = f"{link.context} {link.text}".casefold()
    return bool(
        re.search(
            r"(?:family|section|division|group|selector|combo|tur[_-]?sel)",
            context,
        )
        or re.search(r"\bu\d{2}\b", link.text, re.IGNORECASE)
    )


class ChessResultsDiscovery:
    """Stateless, read-only discovery service for Chess-Results."""

    def __init__(
        self,
        timeout_sec: float = 30.0,
        opener: Any = None,
        base_url: str = _BASE_URL,
    ) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        self.timeout_sec = float(timeout_sec)
        self.base_url = base_url.rstrip("/")
        _origin(self.base_url)
        if opener is None:
            opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.opener = opener

    def _fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
    ) -> tuple[str, bytes]:
        _origin(url)
        request = Request(
            url,
            data=data,
            headers={"User-Agent": "ChessGrandmaster discovery/1.0"},
            method=method,
        )
        try:
            if hasattr(self.opener, "open"):
                response = self.opener.open(request, timeout=self.timeout_sec)
            else:
                response = self.opener(request, timeout=self.timeout_sec)
            try:
                status = getattr(response, "status", None)
                if status is not None and int(status) >= 400:
                    raise RuntimeError(f"Chess-Results HTTP error {status}")
                final_url = str(getattr(response, "geturl", lambda: url)() or url)
                # Chess-Results may redirect the bare host to an approved shard.
                _origin(final_url)
                body = response.read(_MAX_HTML_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except HTTPError as exc:
            raise RuntimeError(f"Chess-Results HTTP error {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError("Chess-Results request timed out") from exc
        except URLError as exc:
            raise RuntimeError("Chess-Results request failed") from exc
        except OSError as exc:
            raise RuntimeError("Chess-Results request failed") from exc
        if len(body) > _MAX_HTML_BYTES:
            raise RuntimeError("Chess-Results page exceeds discovery size limit")
        return final_url, body

    @staticmethod
    def _federation_url(base_url: str, federation: str) -> str:
        if not re.fullmatch(r"[A-Za-z]{3}", federation):
            raise ValueError("federation must be a three-letter FIDE abbreviation")
        return f"{base_url}{_FEDERATION_PATH}?lan=1&fed={federation.upper()}"

    def discover_federation(
        self, federation: str, limit: int | None = None
    ) -> DiscoveryResult:
        """Discover tournament links from one provider federation feed."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        federation = federation.strip().upper()
        page_url, body = self._fetch(self._federation_url(self.base_url, federation))
        parser = _parse_discovery_page(body)
        candidates: list[ChessResultsCandidate] = []
        for row in parser.rows:
            row_text = " ".join(row.cells)
            for link in row.links:
                parsed = _tnr_from_href(page_url, link.href)
                if parsed is None:
                    continue
                external_id, source_url = parsed
                candidates.append(
                    ChessResultsCandidate(
                        provider=PROVIDER,
                        external_id=external_id,
                        source_url=source_url,
                        title=link.text or None,
                        time_control_hint=_time_control(f"{row_text} {link.text}"),
                        evidence=(f"federation_feed:{federation}",),
                    )
                )
        if not candidates:
            for link in parser.anchors:
                parsed = _tnr_from_href(page_url, link.href)
                if parsed is None:
                    continue
                external_id, source_url = parsed
                candidates.append(
                    ChessResultsCandidate(
                        provider=PROVIDER,
                        external_id=external_id,
                        source_url=source_url,
                        title=link.text or None,
                        time_control_hint=_time_control(link.text),
                        evidence=(f"federation_feed:{federation}",),
                    )
                )
        return DiscoveryResult(_ordered_unique(candidates, limit=limit))

    def _corpus_search_url(self) -> str:
        return f"{self.base_url}/TurnierSuche.aspx?lan=1"

    def _query_corpus_window(
        self,
        *,
        year: int,
        from_date: str,
        to_date: str,
        max_lines: int,
        country: str | None,
    ) -> tuple[list[ChessResultsCandidate], int, int]:
        entry_page_url, entry_body = self._fetch(self._corpus_search_url())
        entry = _parse_discovery_page(entry_body)
        form, controls, values = _corpus_form(
            entry,
            max_lines=max_lines,
            country=country,
        )
        overrides = {
            controls["from"].name: from_date,
            controls["to"].name: to_date,
            controls["time_control"].name: values["time_control"],
            controls["finished"].name: values["finished"],
            controls["games_available"].name: values["games_available"],
            controls["max_lines"].name: values["max_lines"],
            controls["sort"].name: values["sort"],
            controls["country"].name: values["country"],
        }
        result_url, method, data = _form_request(
            form,
            entry_page_url,
            controls["submit"],
            overrides,
        )
        result_page_url, result_body = self._fetch(
            result_url,
            method=method,
            data=data,
        )
        candidates, provider_result_row_count = _corpus_candidates(
            result_page_url,
            _parse_discovery_page(result_body),
            year=year,
            window_from=from_date,
            window_to=to_date,
        )
        return candidates, provider_result_row_count, len(candidates)

    def _scan_corpus_window(
        self,
        *,
        year: int,
        from_date: str,
        to_date: str,
        max_lines: int,
        country: str | None,
    ) -> tuple[list[ChessResultsCandidate], list[CorpusWindow]]:
        candidates, provider_result_row_count, parsed_candidate_count = (
            self._query_corpus_window(
            year=year,
            from_date=from_date,
            to_date=to_date,
            max_lines=max_lines,
            country=country,
            )
        )
        if provider_result_row_count < max_lines:
            return candidates, [
                CorpusWindow(
                    from_date,
                    to_date,
                    provider_result_row_count,
                    parsed_candidate_count=parsed_candidate_count,
                )
            ]
        if from_date == to_date:
            return candidates, [
                CorpusWindow(
                    from_date,
                    to_date,
                    provider_result_row_count,
                    saturated=True,
                    parsed_candidate_count=parsed_candidate_count,
                )
            ]
        left, right = _split_date_window(from_date, to_date)
        left_candidates, left_windows = self._scan_corpus_window(
            year=year,
            from_date=left[0],
            to_date=left[1],
            max_lines=max_lines,
            country=country,
        )
        right_candidates, right_windows = self._scan_corpus_window(
            year=year,
            from_date=right[0],
            to_date=right[1],
            max_lines=max_lines,
            country=country,
        )
        return (
            [*left_candidates, *right_candidates],
            [
                CorpusWindow(
                    from_date,
                    to_date,
                    provider_result_row_count,
                    split=True,
                    parsed_candidate_count=parsed_candidate_count,
                ),
                *left_windows,
                *right_windows,
            ],
        )

    def discover_corpus(
        self,
        *,
        year: int,
        max_lines: int = 2000,
        limit: int | None = None,
        country: str | None = None,
    ) -> CorpusDiscoveryResult:
        """Scan the complete year through Tournament Database windows."""
        if isinstance(year, bool) or not isinstance(year, int):
            raise ValueError("year must be an integer")
        if year < 1 or year > 9999:
            raise ValueError("year must be between 1 and 9999")
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        if country is not None:
            country = country.strip().upper()
            if not re.fullmatch(r"[A-Z]{3}", country):
                raise ValueError("country must be a three-letter FIDE abbreviation")

        all_candidates: list[ChessResultsCandidate] = []
        windows: list[CorpusWindow] = []
        errors: list[str] = []
        for from_date, to_date in _calendar_windows(year):
            try:
                candidates, scanned_windows = self._scan_corpus_window(
                    year=year,
                    from_date=from_date,
                    to_date=to_date,
                    max_lines=max_lines,
                    country=country,
                )
            except Exception as exc:
                errors.append(
                    f"source_window:{from_date}:{to_date}:{type(exc).__name__}:{exc}"
                )
                continue
            all_candidates.extend(candidates)
            windows.extend(scanned_windows)
        return CorpusDiscoveryResult(
            candidates=_ordered_unique(all_candidates, limit=limit),
            windows=tuple(windows),
            errors=tuple(errors),
        )

    def enrich_corpus_priority(
        self,
        result: CorpusDiscoveryResult,
        *,
        year: int,
        limit: int | None = None,
        priority_results: tuple[DiscoveryResult, ...] | None = None,
    ) -> CorpusDiscoveryResult:
        """Add existing player-discovery evidence after the full scan."""
        if priority_results is None:
            from_date = f"{year:04d}-01-01"
            to_date = f"{year:04d}-12-31"
            discovered: list[DiscoveryResult] = []
            errors = list(result.errors)
            for lane, discover in (
                (
                    "overseas-vie",
                    lambda: self.discover_overseas_vie(
                        from_date=from_date,
                        to_date=to_date,
                    ),
                ),
                (
                    "diaspora",
                    lambda: self.discover_diaspora(
                        from_date=from_date,
                        to_date=to_date,
                    ),
                ),
            ):
                try:
                    lane_result = discover()
                except Exception as exc:
                    errors.append(
                        f"priority:{lane}:{type(exc).__name__}:{exc}"
                    )
                    continue
                discovered.append(lane_result)
                errors.extend(f"priority:{lane}:{error}" for error in lane_result.errors)
            priority_results = tuple(discovered)
        else:
            errors = list(result.errors)
            for index, lane_result in enumerate(priority_results):
                errors.extend(
                    f"priority:{index}:{error}" for error in lane_result.errors
                )
        return CorpusDiscoveryResult(
            candidates=merge_corpus_priority(
                result.candidates,
                priority_results,
                limit=limit,
            ),
            windows=result.windows,
            errors=tuple(errors),
        )

    def _player_search_url(self) -> str:
        return f"{self.base_url}{_PLAYER_SEARCH_PATH}?lan=1"

    @staticmethod
    def _validate_date(value: str | None, field_name: str) -> str | None:
        if value is None:
            return None
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be YYYY-MM-DD") from exc
        return value

    def discover_player(
        self,
        fide_id: int | str,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int | None = None,
    ) -> DiscoveryResult:
        """Discover explicit tournament rows for one FIDE ID."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        if isinstance(fide_id, bool):
            raise ValueError("fide_id must be numeric")
        fide_text = str(fide_id).strip()
        if not fide_text.isdigit() or int(fide_text) <= 0:
            raise ValueError("fide_id must be numeric")
        from_date = self._validate_date(from_date, "from_date")
        to_date = self._validate_date(to_date, "to_date")
        if from_date and to_date and from_date > to_date:
            raise ValueError("from_date must not be after to_date")

        entry_url = self._player_search_url()
        entry_page_url, entry_body = self._fetch(entry_url)
        entry = _parse_discovery_page(entry_body)
        form, fide_control, submit = _find_player_form(entry)
        overrides = {fide_control.name: fide_text}
        for control in form.controls:
            semantic = control.semantic_text
            compact = re.sub(r"[^a-z0-9]", "", semantic)
            words = _control_words(control)
            if from_date and (
                words & {"from", "von"} or "txtvontag" in compact
            ) and control.input_type in {"date", "text"}:
                overrides[control.name] = from_date
            if to_date and (
                words & {"to", "bis"} or "txtbistag" in compact
            ) and control.input_type in {"date", "text"}:
                overrides[control.name] = to_date
        submit_url, method, data = _form_request(
            form, entry_page_url, submit, overrides
        )
        result_page_url, result_body = self._fetch(
            submit_url,
            method=method,
            data=data,
        )
        result_page = _parse_discovery_page(result_body)
        candidates = _player_candidates(
            result_page_url,
            result_page,
            (f"player_database:fide_id:{int(fide_text)}",),
        )
        return DiscoveryResult(_ordered_unique(candidates, limit=limit))

    def discover_overseas_vie(
        self,
        *,
        from_date: str,
        to_date: str,
        limit: int | None = None,
    ) -> DiscoveryResult:
        """Discover recent overseas tournament rows for FED=VIE players."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        from_date = self._validate_date(from_date, "from_date")
        to_date = self._validate_date(to_date, "to_date")
        if from_date is None or to_date is None:
            raise ValueError("from_date and to_date are required")
        if from_date > to_date:
            raise ValueError("from_date must not be after to_date")

        entry_page_url, entry_body = self._fetch(self._player_search_url())
        entry = _parse_discovery_page(entry_body)
        form, _, submit = _find_player_form(entry)
        fed_control = _source_fed_control(form)
        overseas_control = _overseas_control(form)
        overrides = {
            fed_control.name: "VIE",
            overseas_control.name: overseas_control.value or "on",
        }
        for control in form.controls:
            semantic = control.semantic_text
            compact = re.sub(r"[^a-z0-9]", "", semantic)
            words = _control_words(control)
            if (
                words & {"from", "von"} or "txtvontag" in compact
            ) and control.input_type in {"date", "text"}:
                overrides[control.name] = from_date
            if (
                words & {"to", "bis"} or "txtbistag" in compact
            ) and control.input_type in {"date", "text"}:
                overrides[control.name] = to_date
        submit_url, method, data = _form_request(
            form, entry_page_url, submit, overrides
        )
        result_page_url, result_body = self._fetch(
            submit_url,
            method=method,
            data=data,
        )
        candidates = _player_candidates(
            result_page_url,
            _parse_discovery_page(result_body),
            ("player_database:overseas_vie",),
        )
        return DiscoveryResult(_ordered_unique(candidates, limit=limit))


    def discover_diaspora(
        self,
        *,
        from_date: str,
        to_date: str,
        limit: int | None = None,
        surname_seeds: tuple[str, ...] | None = None,
    ) -> DiscoveryResult:
        """Search bounded surname seeds and keep only strong name hints."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        from_date = self._validate_date(from_date, "from_date")
        to_date = self._validate_date(to_date, "to_date")
        if from_date is None or to_date is None:
            raise ValueError("from_date and to_date are required")
        if from_date > to_date:
            raise ValueError("from_date must not be after to_date")
        seeds = tuple(sorted(set(surname_seeds or VIETNAMESE_SURNAMES)))
        if not seeds:
            raise ValueError("surname_seeds must not be empty")

        candidates: list[ChessResultsCandidate] = []
        errors: list[str] = []
        for seed in seeds:
            if not seed or not re.fullmatch(r"[a-z0-9-]+", seed.casefold()):
                errors.append(f"surname_seed:{seed}:invalid seed")
                continue
            try:
                entry_page_url, entry_body = self._fetch(self._player_search_url())
                entry = _parse_discovery_page(entry_body)
                form, _, submit = _find_player_form(entry)
                surname_control = _surname_control(form)
                overrides = {surname_control.name: seed}
                for control in form.controls:
                    compact = re.sub(r"[^a-z0-9]", "", control.semantic_text)
                    words = _control_words(control)
                    if (
                        words & {"from", "von"} or "txtvontag" in compact
                    ) and control.input_type in {"date", "text"}:
                        overrides[control.name] = from_date
                    if (
                        words & {"to", "bis"} or "txtbistag" in compact
                    ) and control.input_type in {"date", "text"}:
                        overrides[control.name] = to_date
                submit_url, method, data = _form_request(
                    form, entry_page_url, submit, overrides
                )
                result_page_url, result_body = self._fetch(
                    submit_url,
                    method=method,
                    data=data,
                )
                candidates.extend(
                    _player_candidates(
                        result_page_url,
                        _parse_discovery_page(result_body),
                        ("player_database:diaspora",),
                        strong_only=True,
                        surname_seed=seed,
                    )
                )
            except (RuntimeError, ValueError) as exc:
                errors.append(f"surname_seed:{seed}:{exc}")
        return DiscoveryResult(_ordered_unique(candidates, limit=limit), tuple(errors))


    def expand_family(
        self,
        seed: str,
        *,
        limit: int | None = None,
    ) -> DiscoveryResult:
        """Follow only explicit family links or provider relation controls."""
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        seed = seed.strip()
        if re.fullmatch(r"tnr\d+", seed, re.IGNORECASE):
            seed_url = f"{self.base_url}/{seed.lower()}.aspx?lan=1"
        else:
            seed_url = seed
        _origin(seed_url)
        seed_page_url, body = self._fetch(seed_url)
        seed_match = _tnr_from_href(seed_page_url, seed_page_url)
        seed_id = seed_match[0] if seed_match else None
        parser = _parse_discovery_page(body)
        candidates: list[ChessResultsCandidate] = []
        relation_links = [
            (link, "family_relation:explicit_link")
            for link in parser.anchors
            if _family_relation_allowed(link)
        ]
        relation_links.extend(
            (link, "family_relation:explicit_control")
            for link in parser.relation_options
            if _family_relation_allowed(link)
        )
        for link, evidence in relation_links:
            href = link.href
            if re.fullmatch(r"tnr\d+", href, re.IGNORECASE):
                href = f"/{href.lower()}.aspx?lan=1"
            parsed = _tnr_from_href(seed_page_url, href)
            if parsed is None or parsed[0] == seed_id:
                continue
            external_id, source_url = parsed
            candidates.append(
                ChessResultsCandidate(
                    provider=PROVIDER,
                    external_id=external_id,
                    source_url=source_url,
                    title=link.text or None,
                    time_control_hint=_time_control(f"{link.context} {link.text}"),
                    evidence=(evidence,),
                )
            )
        errors = () if candidates else ("family:no explicit provider relation",)
        return DiscoveryResult(_ordered_unique(candidates, limit=limit), errors)


__all__ = [
    "ChessResultsCandidate",
    "ChessResultsDiscovery",
    "CorpusDiscoveryResult",
    "CorpusWindow",
    "DiscoveryResult",
    "PlayerEvidence",
    "PROVIDER",
    "merge_corpus_priority",
]
