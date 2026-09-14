"""Direct URL/source adapter for Chess-Results tournament pages."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import socket
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, build_opener

from .base import SourceDescriptor, SourceRef


PROVIDER = "chess-results"
USER_AGENT = "ChessGrandmaster acquisition/1.0"
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
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


@dataclass(frozen=True)
class _Anchor:
    href: str | None
    text: str


class _PageParser(HTMLParser):
    """Collect only headings, anchors, and visible text from a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self.page_titles: list[str] = []
        self.anchors: list[_Anchor] = []
        self._visible_parts: list[str] = []
        self._capture_stack: list[tuple[str, list[str]]] = []
        self._anchor_stack: list[tuple[str | None, list[str]]] = []
        self._ignored_depth = 0

    @staticmethod
    def _normalize_text(parts: list[str]) -> str:
        return " ".join(" ".join(parts).split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return

        if tag in {"title", "h1", "h2"}:
            self._capture_stack.append((tag, []))
        if tag == "a":
            href = dict(attrs).get("href")
            self._anchor_stack.append((href, []))

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

    def finish(self) -> None:
        """Retain useful text when a malformed page omits closing tags."""
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


def _supported_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.casefold().rstrip(".")
    return normalized in {
        "chess-results.com",
        "www.chess-results.com",
    } or bool(_NUMBERED_HOST_RE.fullmatch(normalized))


def _external_id_from_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("Chess-Results source URL must use http or https")
    if not _supported_host(parsed.hostname):
        raise ValueError("source URL is not a supported Chess-Results host")
    match = _PATH_TNR_RE.fullmatch(parsed.path.rsplit("/", 1)[-1])
    if match is None:
        raise ValueError("source URL must end with tnrNNNN.aspx")
    return f"tnr{match.group('number')}"


def _pgn_candidate(page_url: str, anchor: _Anchor) -> str | None:
    if not anchor.href:
        return None
    candidate = urljoin(page_url, anchor.href)
    parsed = urlparse(candidate)
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None

    text = anchor.text.casefold()
    exposed_url_text = unquote(
        f"{parsed.path}?{parsed.query}"
    ).casefold()
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


class ChessResultsAdapter:
    """Discover and download directly exposed Chess-Results PGN files."""

    def __init__(self, timeout_sec: float = 30.0, opener: Any = None) -> None:
        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        self.timeout_sec = float(timeout_sec)
        self.opener = opener if opener is not None else build_opener()

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
        if ref.provider != PROVIDER:
            raise ValueError("source ref provider is not chess-results")
        if _external_id_from_url(ref.source_url) != ref.external_id:
            raise ValueError("source ref external_id does not match source_url")
        return ref

    def _open_response(self, url: str):
        request = Request(url, headers={"User-Agent": USER_AGENT})
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
        status = getattr(response, "status", None)
        if status is None:
            getcode = getattr(response, "getcode", None)
            if callable(getcode):
                status = getcode()
        if status is not None and int(status) >= 400:
            raise RuntimeError(f"Chess-Results HTTP error {status}")

    def _fetch_page(self, page_url: str) -> str:
        response = self._open_response(page_url)
        try:
            self._check_response(response)
            body = response.read()
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
        try:
            return bytes(body).decode("utf-8", errors="replace")
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("Chess-Results page is malformed") from exc

    def describe(self, ref: SourceRef) -> SourceDescriptor:
        """Fetch a page and return only metadata/link evidence it exposes."""
        ref = self._validate_ref(ref)
        html = self._fetch_page(ref.source_url)
        parser = _PageParser()
        try:
            parser.feed(html)
            parser.close()
            parser.finish()
        except Exception as exc:
            raise RuntimeError("Chess-Results page is malformed") from exc

        if not parser.visible_text and not parser.anchors:
            raise RuntimeError("Chess-Results page is empty or malformed")

        title = next(
            (candidate for candidate in parser.headings if candidate),
            next((candidate for candidate in parser.page_titles if candidate), None),
        )
        pgn_url = next(
            (
                candidate
                for anchor in parser.anchors
                if (candidate := _pgn_candidate(ref.source_url, anchor)) is not None
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

    def download_pgn(self, ref: SourceRef, destination: Path) -> Path:
        """Stream an exposed PGN through a sibling part file atomically."""
        destination = Path(destination).expanduser()
        part_path = Path(f"{destination}.part")
        response = None
        try:
            descriptor = self.describe(ref)
            if descriptor.pgn_url is None:
                raise RuntimeError(
                    "Chess-Results page exposes no downloadable PGN link"
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            response = self._open_response(descriptor.pgn_url)
            self._check_response(response)
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
                    output.write(block)
                    byte_count += len(block)
                if byte_count == 0:
                    raise RuntimeError("Chess-Results PGN download was empty")
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


__all__ = ["ChessResultsAdapter", "PROVIDER", "USER_AGENT"]
