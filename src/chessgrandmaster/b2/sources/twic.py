"""The Week in Chess issue 1660+ source adapter."""

from __future__ import annotations

from html.parser import HTMLParser
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlparse
import zipfile

from ._common import (
    DOWNLOAD_CHUNK_SIZE,
    PGN_PROBE_BYTES,
    ResponseSnapshot,
    approved_url,
    content_disposition_filename,
    content_type,
    ensure_pgn_signature,
    fetch_body,
    final_response_url,
    make_opener,
    object_text,
    open_response,
    response_header,
    response_status,
    same_origin,
)
from .base import SourceDescriptor, SourceRef


PROVIDER = "twic"
USER_AGENT = "ChessGrandmaster acquisition/1.0"
_DEFAULT_BASE_URL = "https://theweekinchess.com"
_DEFAULT_HOSTS = {"theweekinchess.com", "www.theweekinchess.com"}
_MIN_ISSUE = 1660
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_MEMBER_BYTES = 256 * 1024 * 1024
_ISSUE_RE = re.compile(
    r"(?i)\btwic[\s._-]*(\d+)\b"
    r"|\bissue(?:[\s._-]*|=)(\d+)\b"
    r"|\bthe\s+week\s+in\s+chess\s+(\d+)\b"
)
_EXTERNAL_RE = re.compile(r"^twic(?P<issue>\d+)$", re.IGNORECASE)


class _ArchiveParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.headings: list[str] = []
        self.anchors: list[tuple[str | None, str]] = []
        self._capture: list[tuple[str, list[str]]] = []
        self._anchor: tuple[str | None, list[str]] | None = None

    @staticmethod
    def _text(parts: list[str]) -> str:
        return " ".join(" ".join(parts).split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.casefold(): value or "" for key, value in attrs}
        lower = tag.casefold()
        if lower in {"title", "h1", "h2"}:
            self._capture.append((lower, []))
        if lower == "a":
            self._anchor = (attrs_map.get("href"), [])

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower == "a" and self._anchor is not None:
            href, parts = self._anchor
            self.anchors.append((href, self._text(parts)))
            self._anchor = None
        for index in range(len(self._capture) - 1, -1, -1):
            capture_tag, parts = self._capture[index]
            if capture_tag != lower:
                continue
            self._capture.pop(index)
            text = self._text(parts)
            if not text:
                break
            if lower == "title":
                self.title_parts.append(text)
            else:
                self.headings.append(text)
            break

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor[1].append(data)
        for _, parts in self._capture:
            parts.append(data)

    def finish(self) -> None:
        if self._anchor is not None:
            href, parts = self._anchor
            self.anchors.append((href, self._text(parts)))
            self._anchor = None
        for tag, parts in self._capture:
            text = self._text(parts)
            if not text:
                continue
            if tag == "title":
                self.title_parts.append(text)
            else:
                self.headings.append(text)
        self._capture.clear()

    @property
    def title(self) -> str | None:
        return next(iter(self.title_parts), None) or next(iter(self.headings), None)


def _issue_from_text(value: str) -> int | None:
    match = _ISSUE_RE.search(value)
    if match is None:
        return None
    return int(next(part for part in match.groups() if part is not None))


def _parse_issue_query(query: str) -> int | None:
    value = query.strip()
    if value.isdigit():
        return int(value)
    match = _EXTERNAL_RE.fullmatch(value)
    if match is not None:
        return int(match.group("issue"))
    if value.casefold().startswith("twic:"):
        tail = value.split(":", 1)[1]
        return int(tail) if tail.isdigit() else None
    if value.startswith("http://") or value.startswith("https://"):
        return _issue_from_text(unquote(urlparse(value).path + "?" + urlparse(value).query))
    return None


def _validate_issue(issue: int) -> int:
    if issue < _MIN_ISSUE:
        raise ValueError(f"TWIC issue must be at least {_MIN_ISSUE}")
    return issue


class TwicAdapter:
    """Acquire immutable PGN bytes from TWIC archive issues 1660 onward."""

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
        approved_url(self.base_url, self.allowed_hosts, "TWIC base URL")
        self.opener, self.cookie_jar = make_opener(opener, session)
        self._records: dict[str, dict[str, Any]] = {}
        self.last_download_filename: str | None = None
        self.last_download_content_type: str | None = None
        self.last_download_content_disposition: str | None = None
        self.last_download_status: int | None = None
        self.last_download_byte_count = 0
        self.last_download_looks_like_pgn = False

    def _archive_url(self, issue: int) -> str:
        return f"{self.base_url}/html/twic{issue}.html"

    def _pgn_zip_url(self, issue: int) -> str:
        return f"{self.base_url}/zips/twic{issue}g.zip"

    def _validate_source_url(self, source_url: str) -> str:
        approved_url(source_url, self.allowed_hosts, "TWIC source URL")
        return source_url

    @staticmethod
    def _is_file_url(url: str) -> bool:
        path = urlparse(url).path.casefold()
        return path.endswith(".pgn") or path.endswith(".zip") or path.endswith(".cbv")

    def discover(self, query: str) -> list[SourceRef]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("TWIC discovery requires an issue number or archive URL")
        query = query.strip()
        issue = _parse_issue_query(query)
        if issue is None:
            raise ValueError("TWIC discovery requires an explicit issue number")
        issue = _validate_issue(issue)
        if query.startswith("http://") or query.startswith("https://"):
            source_url = self._validate_source_url(query)
            if _issue_from_text(unquote(urlparse(source_url).path)) not in {None, issue}:
                raise ValueError("TWIC source URL issue does not match source ref")
        else:
            source_url = self._archive_url(issue)
        return [SourceRef(PROVIDER, f"twic{issue}", source_url)]

    def _validate_ref(self, ref: SourceRef) -> int:
        if not isinstance(ref, SourceRef):
            raise TypeError("ref must be a SourceRef")
        if ref.provider != PROVIDER:
            raise ValueError("source ref provider is not twic")
        match = _EXTERNAL_RE.fullmatch(ref.external_id)
        if match is None:
            raise ValueError("TWIC external_id must match twicNNNN")
        issue = _validate_issue(int(match.group("issue")))
        self._validate_source_url(ref.source_url)
        from_url = _issue_from_text(unquote(urlparse(ref.source_url).path))
        if from_url is not None and from_url != issue:
            raise ValueError("TWIC source URL issue does not match source ref")
        return issue

    def _links(self, page_url: str, parser: _ArchiveParser, issue: int) -> list[str]:
        candidates: list[tuple[int, str]] = []
        for href, text in parser.anchors:
            if not href:
                continue
            candidate = urljoin(page_url, href)
            try:
                same_origin(page_url, candidate, self.allowed_hosts, "TWIC archive link")
            except ValueError:
                continue
            path = urlparse(candidate).path.casefold()
            if not (path.endswith(".pgn") or path.endswith(".zip") or path.endswith(".cbv")):
                continue
            link_issue = _issue_from_text(unquote(path + " " + text))
            if link_issue is not None and link_issue != issue:
                continue
            # Prefer a direct PGN, then a zip/cbv archive; both are valid inputs.
            score = 0 if path.endswith(".pgn") else 1
            candidates.append((score, candidate))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return [candidate for _, candidate in candidates]

    def _cache(self, issue: int, source_url: str, pgn_url: str, *, title: str | None, text: str) -> None:
        self._records[f"twic{issue}"] = {
            "issue": issue,
            "source_url": source_url,
            "pgn_url": pgn_url,
            "title": title,
            "text": text,
        }

    def describe(self, ref: SourceRef) -> SourceDescriptor:
        issue = self._validate_ref(ref)
        deterministic_pgn_url = self._pgn_zip_url(issue)
        path = urlparse(ref.source_url).path.casefold()
        if self._is_file_url(ref.source_url):
            record = {
                "issue": issue,
                "source_url": ref.source_url,
                "pgn_url": deterministic_pgn_url,
                "title": f"The Week in Chess {issue}",
                "text": "",
            }
            self._records[f"twic{issue}"] = record
        else:
            try:
                fetched = fetch_body(
                    self.opener,
                    ref.source_url,
                    timeout=self.timeout_sec,
                    allowed_hosts=self.allowed_hosts,
                    user_agent=USER_AGENT,
                    provider="TWIC",
                )
            except RuntimeError:
                # HTML is optional metadata; the deterministic ZIP remains
                # available when the archive page is temporarily unavailable.
                record = {
                    "issue": issue,
                    "source_url": ref.source_url,
                    "pgn_url": deterministic_pgn_url,
                    "title": f"The Week in Chess {issue}",
                    "text": "",
                }
                self._records[f"twic{issue}"] = record
            else:
                text = object_text(fetched.body)
                parser = _ArchiveParser()
                try:
                    parser.feed(text)
                    parser.close()
                    parser.finish()
                except Exception as exc:
                    raise RuntimeError("TWIC archive page is malformed") from exc
                text_issue = _issue_from_text(" ".join([parser.title or "", *parser.headings, text]))
                if text_issue is not None and text_issue != issue:
                    raise ValueError("TWIC archive page issue does not match source ref")
                # The canonical ZIP URL is deterministic; HTML links are optional
                # metadata and must not select a different download contract.
                self._cache(
                    issue,
                    ref.source_url,
                    deterministic_pgn_url,
                    title=parser.title,
                    text=text,
                )
                record = self._records[f"twic{issue}"]
        text = str(record.get("text", ""))
        lower_text = text.casefold()
        control = None
        if re.search(r"\bclassical\b|\bstandard\b", lower_text):
            control = "classical"
        is_otb: bool | None = None
        if re.search(r"\bover[ -]the[ -]board\b|\botb\b", lower_text):
            is_otb = True
        return SourceDescriptor(
            ref=ref,
            title=record.get("title"),
            pgn_url=record["pgn_url"],
            event_country_hint=None,
            time_control_hint=control,
            is_otb_hint=is_otb,
        )

    def _record(self, ref: SourceRef, issue: int) -> dict[str, Any]:
        record = self._records.get(f"twic{issue}")
        if record is not None:
            return record
        self.describe(ref)
        return self._records[f"twic{issue}"]

    def _warm_up_archive_index(self, issue: int) -> None:
        try:
            fetch_body(
                self.opener,
                self._archive_url(issue),
                timeout=self.timeout_sec,
                allowed_hosts=self.allowed_hosts,
                user_agent=USER_AGENT,
                provider="TWIC",
            )
        except RuntimeError:
            # The warm-up is bounded and only primes provider-side filtering;
            # the deterministic ZIP retry remains the authoritative operation.
            pass

    @staticmethod
    def _is_http_406(error: RuntimeError) -> bool:
        return bool(re.search(r"\b406\b", str(error)))

    def _download_archive(self, url: str, destination: Path, issue: int) -> ResponseSnapshot:
        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        archive_part = Path(f"{destination}.archive.part")
        output_part = Path(f"{destination}.part")
        response = None
        status: int | None = None
        response_type: str | None = None
        disposition: str | None = None
        filename: str | None = None
        byte_count = 0
        prefix = bytearray()
        try:
            response = open_response(
                self.opener,
                url,
                timeout=self.timeout_sec,
                user_agent=USER_AGENT,
                provider="TWIC",
            )
            status = response_status(response)
            if status is not None and status >= 400:
                raise RuntimeError(f"TWIC HTTP error {status}")
            final_url = final_response_url(response, url)
            same_origin(url, final_url, self.allowed_hosts, "TWIC archive final response URL")
            response_type = content_type(response)
            disposition = response_header(response, "Content-Disposition")
            filename = content_disposition_filename(response)
            with archive_part.open("wb") as output:
                while True:
                    block = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not isinstance(block, (bytes, bytearray)):
                        raise RuntimeError("TWIC archive response returned non-byte data")
                    if not block:
                        break
                    block = bytes(block)
                    if len(prefix) < PGN_PROBE_BYTES:
                        prefix.extend(block[: PGN_PROBE_BYTES - len(prefix)])
                    output.write(block)
                    byte_count += len(block)
                    if byte_count > _MAX_ARCHIVE_BYTES:
                        raise RuntimeError("TWIC archive response exceeded size limit")
                if byte_count == 0:
                    raise RuntimeError("TWIC archive response was empty")
                if response_type == "text/html" or prefix.lstrip().lower().startswith((b"<html", b"<!doctype")):
                    raise RuntimeError("TWIC response was HTML, not a PGN archive")
                output.flush()
                os.fsync(output.fileno())
            if not bytes(prefix).startswith(b"PK") or not zipfile.is_zipfile(archive_part):
                raise RuntimeError("TWIC response was not a PGN or ZIP archive")
            with zipfile.ZipFile(archive_part) as archive:
                members = [
                    info
                    for info in archive.infolist()
                    if not info.is_dir() and info.filename.casefold().endswith(".pgn")
                ]
                preferred = [
                    info for info in members
                    if str(issue) in Path(info.filename).stem
                ]
                if preferred:
                    members = preferred
                if not members:
                    raise RuntimeError("TWIC archive contains no PGN member")
                member = sorted(members, key=lambda info: info.filename.casefold())[0]
                if member.file_size > _MAX_MEMBER_BYTES:
                    raise RuntimeError("TWIC PGN member exceeded size limit")
                member_prefix = bytearray()
                member_size = 0
                with archive.open(member) as source, output_part.open("wb") as output:
                    while True:
                        block = source.read(DOWNLOAD_CHUNK_SIZE)
                        if not block:
                            break
                        if not isinstance(block, bytes):
                            raise RuntimeError("TWIC PGN member returned non-byte data")
                        if len(member_prefix) < PGN_PROBE_BYTES:
                            member_prefix.extend(block[: PGN_PROBE_BYTES - len(member_prefix)])
                        output.write(block)
                        member_size += len(block)
                    ensure_pgn_signature(bytes(member_prefix), "text/plain", "TWIC")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(output_part, destination)
            return ResponseSnapshot(
                url=final_url,
                status=status,
                content_type=response_type,
                content_disposition=disposition,
                filename=filename,
                byte_count=byte_count,
            )
        except RuntimeError:
            raise
        except (TimeoutError, OSError, ValueError, zipfile.BadZipFile) as exc:
            raise RuntimeError(f"TWIC PGN archive download failed: {exc}") from exc
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            for path in (archive_part, output_part):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def download_pgn(self, ref: SourceRef, destination: Path) -> Path:
        issue = self._validate_ref(ref)
        self._record(ref, issue)
        pgn_url = self._pgn_zip_url(issue)
        same_origin(ref.source_url, pgn_url, self.allowed_hosts, "TWIC PGN URL")
        try:
            snapshot = self._download_archive(pgn_url, Path(destination), issue)
        except RuntimeError as error:
            if not self._is_http_406(error):
                raise
            self._warm_up_archive_index(issue)
            snapshot = self._download_archive(pgn_url, Path(destination), issue)
        self.last_download_filename = snapshot.filename
        self.last_download_content_type = snapshot.content_type
        self.last_download_content_disposition = snapshot.content_disposition
        self.last_download_status = snapshot.status
        self.last_download_byte_count = snapshot.byte_count
        self.last_download_looks_like_pgn = True
        return Path(destination)


TWICAdapter = TwicAdapter


__all__ = ["PROVIDER", "TWICAdapter", "TwicAdapter", "USER_AGENT"]
