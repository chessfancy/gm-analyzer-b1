"""HTTP-backed mailbox transport for the Molab coordinator bridge."""
from __future__ import annotations

import hashlib
import http.client
import os
from pathlib import Path
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class HTTPMailboxStorage:
    def __init__(self, *, base_url: str, token: str, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = int(timeout)
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("relay base_url must be http(s)")

    def _url(self, key: str) -> str:
        if not key or key.startswith("/") or "\\" in key or ".." in key.split("/"):
            raise ValueError(f"unsafe mailbox key: {key!r}")
        return self.base_url + "/objects/" + urllib.parse.quote(key, safe="/")

    def _request(self, key: str, *, method: str = "GET", data=None, headers=None):
        merged = {"User-Agent": "ChessGrandmaster/1.0"}
        if self.token:
            merged["Authorization"] = f"Bearer {self.token}"
        if headers: merged.update(headers)
        request = urllib.request.Request(self._url(key), data=data, method=method, headers=merged)
        try:
            return urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError(f"relay denied {method} {key}") from exc
            raise

    def read_bytes(self, path: str) -> bytes | None:
        try:
            with self._request(path) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404: return None
            raise

    def download(self, path: str, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".part")
        os.close(fd); temp = Path(name)
        try:
            with self._request(path) as response, temp.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    handle.write(chunk)
            os.replace(temp, destination)
        finally:
            try: temp.unlink()
            except FileNotFoundError: pass

    def upload(self, path: str, source: Path) -> None:
        source = Path(source)
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        parsed = urllib.parse.urlsplit(self._url(path))
        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = conn_cls(parsed.hostname, parsed.port, timeout=self.timeout)
        target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
        try:
            connection.putrequest("PUT", target)
            if self.token:
                connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("User-Agent", "ChessGrandmaster/1.0")
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(source.stat().st_size))
            connection.putheader("X-CGM-SHA256", digest.hexdigest())
            connection.endheaders()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    connection.send(chunk)
            response = connection.getresponse()
            body = response.read()
            if response.status in (401, 403):
                raise PermissionError(f"relay denied PUT {path}")
            if not 200 <= response.status < 300:
                raise OSError(f"relay PUT {path} failed HTTP {response.status}: {body[:200]!r}")
        finally:
            connection.close()

    def delete(self, path: str) -> None:
        with self._request(path, method="DELETE") as response:
            response.read()
