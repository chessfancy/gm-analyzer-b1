"""Small authenticated HTTP mailbox relay for Molab worker transport."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import urllib.parse

MAX_OBJECT_BYTES = 2_000_000_000


def _safe_key(raw: str) -> str:
    value = urllib.parse.unquote(raw)
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid object key")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("unsafe object key")
    if path.parts[0] not in {"inbox", "runtime", "outbox"}:
        raise ValueError("unsupported object prefix")
    return path.as_posix()


def _current_batch(root: Path) -> str | None:
    pointer = root / "inbox/current.json"
    if not pointer.is_file(): return None
    try: return str(json.loads(pointer.read_text(encoding="utf-8")).get("batch_id") or "") or None
    except Exception: return None


def _worker_allowed(root: Path, method: str, key: str) -> bool:
    batch_id = _current_batch(root)
    if not batch_id: return False
    if method == "GET" and key == "inbox/current.json": return True
    if method == "GET" and key == f"inbox/batches/{batch_id}.zip": return True
    if key.startswith(f"runtime/{batch_id}/") and method in {"GET", "PUT"}: return True
    if key == "outbox/ready.json" and method == "PUT": return True
    return False


def build_server(*, root: str | Path, host: str, port: int, admin_token: str = "",
                 admin_token_sha256: str = "", worker_token: str,
                 local_admin_path: bool = False) -> ThreadingHTTPServer:
    storage_root = Path(root).resolve(); storage_root.mkdir(parents=True, exist_ok=True)
    admin_hash = admin_token_sha256.lower().strip()
    if admin_token:
        admin_hash = hashlib.sha256(admin_token.encode()).hexdigest()
    if len(admin_hash) != 64 or any(c not in "0123456789abcdef" for c in admin_hash):
        raise ValueError("admin token or SHA256 must be configured")
    if not worker_token or hmac.compare_digest(admin_hash, hashlib.sha256(worker_token.encode()).hexdigest()):
        raise ValueError("admin and worker tokens must be distinct and non-empty")

    class Handler(BaseHTTPRequestHandler):
        server_version = "CGMRelay/1.0"
        def log_message(self, fmt, *args):
            super().log_message(fmt, *args)
        def _json(self, code: int, payload: dict):
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def _role(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "): return None
            token = auth[7:]
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            if hmac.compare_digest(token_hash, admin_hash): return "admin"
            if hmac.compare_digest(token, worker_token): return "worker"
            return None
        def _object(self):
            worker_prefix = "/v1/objects/"
            admin_prefix = "/oracle/v1/objects/"
            is_local_admin = False
            if self.path.startswith(admin_prefix):
                prefix = admin_prefix
                is_local_admin = True
            elif self.path.startswith(worker_prefix):
                prefix = worker_prefix
            else:
                raise ValueError("not an object path")
            key = _safe_key(self.path[len(prefix):].split("?", 1)[0])
            target = (storage_root / Path(*PurePosixPath(key).parts)).resolve()
            if storage_root != target and storage_root not in target.parents: raise ValueError("unsafe target")
            return key, target, is_local_admin
        def _authorized(self, method: str, key: str, is_local_admin: bool) -> bool:
            if is_local_admin and local_admin_path:
                return True
            role = self._role()
            return role == "admin" or (role == "worker" and _worker_allowed(storage_root, method, key))
        def do_GET(self):
            if self.path == "/healthz": return self._json(200, {"ok": True})
            try: key, target, is_local_admin = self._object()
            except ValueError as exc: return self._json(400, {"error": str(exc)})
            if not self._authorized("GET", key, is_local_admin): return self._json(403, {"error": "forbidden"})
            if not target.is_file(): return self._json(404, {"error": "not found"})
            size = target.stat().st_size; self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size)); self.end_headers()
            with target.open("rb") as handle: shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)
        def do_PUT(self):
            try: key, target, is_local_admin = self._object()
            except ValueError as exc: return self._json(400, {"error": str(exc)})
            if not self._authorized("PUT", key, is_local_admin): return self._json(403, {"error": "forbidden"})
            try: length = int(self.headers.get("Content-Length", "-1"))
            except ValueError: length = -1
            if length < 0 or length > MAX_OBJECT_BYTES: return self._json(413, {"error": "invalid object size"})
            expected = self.headers.get("X-CGM-SHA256", "").lower()
            if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected): return self._json(400, {"error": "missing/invalid X-CGM-SHA256"})
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part"); os.close(fd); temp = Path(name)
            digest = hashlib.sha256(); remaining = length
            try:
                with temp.open("wb") as handle:
                    while remaining:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk: break
                        handle.write(chunk); digest.update(chunk); remaining -= len(chunk)
                if remaining or digest.hexdigest() != expected:
                    return self._json(422, {"error": "checksum mismatch"})
                if key == "outbox/ready.json" and self._role() == "worker":
                    payload = json.loads(temp.read_text(encoding="utf-8")); current = _current_batch(storage_root)
                    if str(payload.get("batch_id") or "") != current: return self._json(403, {"error": "ready batch mismatch"})
                os.replace(temp, target); return self._json(200, {"ok": True, "sha256": expected, "bytes": length})
            finally:
                try: temp.unlink()
                except FileNotFoundError: pass
        def do_DELETE(self):
            try: key, target, is_local_admin = self._object()
            except ValueError as exc: return self._json(400, {"error": str(exc)})
            if not (is_local_admin and local_admin_path) and self._role() != "admin": return self._json(403, {"error": "forbidden"})
            try: target.unlink()
            except FileNotFoundError: pass
            return self._json(200, {"ok": True})
    return ThreadingHTTPServer((host, int(port)), Handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--root", default=os.environ.get("CGM_RELAY_ROOT", "/var/lib/cgm-relay"))
    parser.add_argument("--host", default=os.environ.get("CGM_RELAY_HOST", "127.0.0.1")); parser.add_argument("--port", type=int, default=int(os.environ.get("CGM_RELAY_PORT", "8787")))
    args = parser.parse_args(argv)
    admin = os.environ.get("CGM_RELAY_ADMIN_TOKEN", "")
    admin_hash = os.environ.get("CGM_RELAY_ADMIN_TOKEN_SHA256", "")
    worker = os.environ.get("CGM_RELAY_WORKER_TOKEN", "")
    local_admin = os.environ.get("CGM_RELAY_LOCAL_ADMIN_PATH", "0").strip().lower() in {"1", "true", "yes"}
    server = build_server(root=args.root, host=args.host, port=args.port,
                          admin_token=admin, admin_token_sha256=admin_hash,
                          worker_token=worker, local_admin_path=local_admin)
    server.serve_forever(); return 0

if __name__ == "__main__": raise SystemExit(main())
