"""Small private HTTP/byte helpers shared by live source adapters."""

from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import socket
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .base import NoUsablePgnError


DOWNLOAD_CHUNK_SIZE = 1024 * 1024
PAGE_MAX_BYTES = 8 * 1024 * 1024
PGN_PROBE_BYTES = 64 * 1024


@dataclass(frozen=True)
class ResponseSnapshot:
    """Bounded response metadata retained for the acquisition audit."""

    url: str
    status: int | None
    content_type: str | None
    content_disposition: str | None
    filename: str | None
    byte_count: int


@dataclass(frozen=True)
class FetchedBody:
    """One bounded page response and its final same-origin URL."""

    url: str
    body: bytes
    status: int | None
    content_type: str | None
    content_disposition: str | None


def make_opener(opener: Any = None, session: Any = None) -> tuple[Any, CookieJar | None]:
    """Return an injected opener/session or a cookie-aware urllib opener."""
    if opener is not None and session is not None:
        raise ValueError("pass only one of opener or session")
    injected = opener if opener is not None else session
    if injected is not None:
        return injected, None
    cookie_jar = CookieJar()
    return build_opener(HTTPCookieProcessor(cookie_jar)), cookie_jar


def normalized_hosts(hosts: Iterable[str]) -> frozenset[str]:
    return frozenset(
        host.casefold().rstrip(".")
        for host in hosts
        if isinstance(host, str) and host.strip()
    )


def _parsed_port(parsed) -> int:
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme.casefold() == "https" else 80


def approved_url(
    url: str,
    allowed_hosts: Iterable[str],
    description: str,
) -> str:
    """Validate an HTTP(S), credential-free URL on an approved host."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{description} is not a valid URL") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError(f"{description} must use http or https")
    if has_credentials or not hostname:
        raise ValueError(f"{description} is outside approved source hosts")
    if hostname.casefold().rstrip(".") not in normalized_hosts(allowed_hosts):
        raise ValueError(f"{description} is outside approved source hosts")
    return url


def same_origin(
    base_url: str,
    candidate_url: str,
    allowed_hosts: Iterable[str],
    description: str,
) -> str:
    """Require a candidate URL to remain on the exact observed origin."""
    approved_url(candidate_url, allowed_hosts, description)
    try:
        base = urlparse(base_url)
        candidate = urlparse(candidate_url)
        same = (
            base.scheme.casefold(),
            (base.hostname or "").casefold().rstrip("."),
            _parsed_port(base),
        ) == (
            candidate.scheme.casefold(),
            (candidate.hostname or "").casefold().rstrip("."),
            _parsed_port(candidate),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{description} is not a valid URL") from exc
    if not same:
        raise ValueError(f"{description} is cross-origin")
    return candidate_url


def response_status(response: object) -> int | None:
    value = getattr(response, "status", None)
    if value is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            value = getcode()
    return None if value is None else int(value)


def response_header(response: object, name: str) -> str | None:
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
        items = getattr(headers, "items", None)
        if callable(items):
            for key, value in items():
                if str(key).casefold() == name.casefold():
                    return str(value)
    getheader = getattr(response, "getheader", None)
    if callable(getheader):
        value = getheader(name)
        if value is not None:
            return str(value)
    return None


def content_type(response: object) -> str | None:
    value = response_header(response, "Content-Type")
    if value is None:
        return None
    return value.split(";", 1)[0].strip().casefold() or None


def content_disposition_filename(response: object) -> str | None:
    value = response_header(response, "Content-Disposition")
    if value is None:
        return None
    match = re.search(
        r"filename\*?\s*=\s*(?:UTF-8''([^;]+)|\"([^\"]+)\"|([^;\s]+))",
        value,
        re.IGNORECASE,
    )
    if match is None:
        return None
    for part in match.groups():
        if part:
            from urllib.parse import unquote

            return unquote(part.strip().strip('"')) or None
    return None


def open_response(
    opener: Any,
    url: str,
    *,
    timeout: float,
    method: str = "GET",
    data: bytes | None = None,
    user_agent: str,
    provider: str,
):
    """Open one request while normalizing transport failures."""
    headers = {"User-Agent": user_agent}
    if provider.casefold() == "twic":
        headers.update(
            {
                "Accept": "application/zip,application/octet-stream,*/*",
                "Accept-Encoding": "identity",
                "Referer": "https://theweekinchess.com/",
            }
        )
    if method.upper() == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = Request(url, data=data, headers=headers, method=method.upper())
    try:
        if hasattr(opener, "open"):
            return opener.open(request, timeout=timeout)
        return opener(request, timeout=timeout)
    except HTTPError as exc:
        raise RuntimeError(f"{provider} HTTP error {exc.code}: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"{provider} request timed out") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(f"{provider} request timed out") from exc
        raise RuntimeError(f"{provider} request failed: {reason}") from exc
    except OSError as exc:
        raise RuntimeError(f"{provider} request failed: {exc}") from exc


def close_response(response: object | None) -> None:
    if response is None:
        return
    close = getattr(response, "close", None)
    if callable(close):
        close()


def final_response_url(response: object, requested_url: str) -> str:
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        candidate = geturl()
        if candidate:
            return str(candidate)
    return requested_url


def _check_status(response: object, provider: str) -> int | None:
    status = response_status(response)
    if status is not None and status >= 400:
        raise RuntimeError(f"{provider} HTTP error {status}")
    return status


def fetch_body(
    opener: Any,
    url: str,
    *,
    timeout: float,
    allowed_hosts: Iterable[str],
    user_agent: str,
    provider: str,
    max_bytes: int = PAGE_MAX_BYTES,
) -> FetchedBody:
    """Fetch a bounded metadata page and verify its final same-origin URL."""
    response = open_response(
        opener,
        url,
        timeout=timeout,
        user_agent=user_agent,
        provider=provider,
    )
    response_type: str | None = None
    disposition: str | None = None
    try:
        status = _check_status(response, provider)
        final_url = final_response_url(response, url)
        same_origin(url, final_url, allowed_hosts, f"{provider} final response URL")
        response_type = content_type(response)
        disposition = response_header(response, "Content-Disposition")
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = max_bytes - total
            if remaining <= 0:
                probe = response.read(1)
                if not isinstance(probe, (bytes, bytearray)):
                    raise RuntimeError(f"{provider} metadata response returned non-byte data")
                if probe:
                    raise RuntimeError(f"{provider} metadata response exceeded size limit")
                break
            block = response.read(min(DOWNLOAD_CHUNK_SIZE, remaining))
            if not isinstance(block, (bytes, bytearray)):
                raise RuntimeError(f"{provider} metadata response returned non-byte data")
            if not block:
                break
            total += len(block)
            chunks.append(bytes(block))
    except RuntimeError:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"{provider} metadata request timed out") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{provider} metadata read failed: {exc}") from exc
    finally:
        close_response(response)
    body = b"".join(chunks)
    if not body:
        raise RuntimeError(f"{provider} metadata response was empty")
    return FetchedBody(
        url=final_url,
        body=body,
        status=status,
        content_type=response_type,
        content_disposition=disposition,
    )


def parse_json_body(body: bytes, provider: str) -> object:
    """Parse JSON or newline-delimited JSON without accepting HTML."""
    try:
        value = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"{provider} structured feed is empty")
        try:
            value = [json.loads(line.decode("utf-8")) for line in lines]
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{provider} structured feed is not valid JSON") from exc
    if not isinstance(value, (dict, list)):
        raise ValueError(f"{provider} structured feed must be an object or array")
    return value


def looks_like_html(prefix: bytes) -> bool:
    text = prefix.decode("utf-8", errors="replace").lstrip("\ufeff \t\r\n").casefold()
    return bool(
        text.startswith("<!doctype html")
        or text.startswith("<html")
        or text.startswith("<body")
        or "<html" in text[:512]
    )


def looks_like_pgn(prefix: bytes) -> bool:
    text = prefix.decode("utf-8", errors="replace")
    return bool(
        re.search(r"(?m)^\s*\[[A-Za-z][A-Za-z0-9_]*\s+\"[^\"]*\"\]", text)
        or re.search(r"(?m)^\s*\d+\.(?:\.\.)?\s+\S+", text)
    )


def stream_download(
    opener: Any,
    url: str,
    destination: Path,
    *,
    timeout: float,
    allowed_hosts: Iterable[str],
    user_agent: str,
    provider: str,
    validator: Callable[[bytes, str | None], None] | None = None,
    response_validator: Callable[[object, bytes, str | None], None] | None = None,
) -> ResponseSnapshot:
    """Stream one response to a sibling part file and replace atomically."""
    destination = Path(destination).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    part_path = Path(f"{destination}.part")
    response = None
    status: int | None = None
    response_type: str | None = None
    disposition: str | None = None
    filename: str | None = None
    byte_count = 0
    prefix = bytearray()
    try:
        response = open_response(
            opener,
            url,
            timeout=timeout,
            user_agent=user_agent,
            provider=provider,
        )
        status = _check_status(response, provider)
        final_url = final_response_url(response, url)
        same_origin(url, final_url, allowed_hosts, f"{provider} final response URL")
        response_type = content_type(response)
        disposition = response_header(response, "Content-Disposition")
        filename = content_disposition_filename(response)
        with part_path.open("wb") as output:
            while True:
                block = response.read(DOWNLOAD_CHUNK_SIZE)
                if not isinstance(block, (bytes, bytearray)):
                    raise RuntimeError(f"{provider} download returned non-byte data")
                if not block:
                    break
                block = bytes(block)
                if len(prefix) < PGN_PROBE_BYTES:
                    prefix.extend(block[: PGN_PROBE_BYTES - len(prefix)])
                output.write(block)
                byte_count += len(block)
            if byte_count == 0:
                raise NoUsablePgnError()
            if response_type == "text/html" or looks_like_html(bytes(prefix)):
                raise RuntimeError(f"{provider} response was HTML, not a PGN download")
            if response_validator is not None:
                response_validator(response, bytes(prefix), response_type)
            if validator is not None:
                validator(bytes(prefix), response_type)
            output.flush()
            os.fsync(output.fileno())
        os.replace(part_path, destination)
        return ResponseSnapshot(
            url=final_url,
            status=status,
            content_type=response_type,
            content_disposition=disposition,
            filename=filename,
            byte_count=byte_count,
        )
    except NoUsablePgnError:
        raise
    except RuntimeError:
        raise
    except ValueError:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"{provider} PGN download failed: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{provider} PGN download failed: {exc}") from exc
    finally:
        close_response(response)
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass


def ensure_pgn_signature(prefix: bytes, content_type_value: str | None, provider: str) -> None:
    """Reject successful responses that are not a lightweight PGN."""
    if content_type_value == "text/html" or looks_like_html(prefix):
        raise RuntimeError(f"{provider} response was HTML, not a PGN download")
    if not looks_like_pgn(prefix):
        raise RuntimeError(f"{provider} response did not look like a PGN download")


def object_text(body: bytes) -> str:
    return body.decode("utf-8-sig", errors="replace")


def origin_base(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path="", params="", query="", fragment=""))


__all__ = [
    "DOWNLOAD_CHUNK_SIZE",
    "FetchedBody",
    "PAGE_MAX_BYTES",
    "PGN_PROBE_BYTES",
    "ResponseSnapshot",
    "approved_url",
    "close_response",
    "content_disposition_filename",
    "content_type",
    "ensure_pgn_signature",
    "fetch_body",
    "final_response_url",
    "looks_like_html",
    "looks_like_pgn",
    "make_opener",
    "normalized_hosts",
    "object_text",
    "open_response",
    "origin_base",
    "parse_json_body",
    "response_header",
    "response_status",
    "same_origin",
    "stream_download",
]
