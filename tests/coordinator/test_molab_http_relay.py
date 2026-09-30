from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading


def test_http_relay_storage_round_trip_and_worker_scope(tmp_path: Path):
    from chessgrandmaster.coordinator.http_mailbox import HTTPMailboxStorage
    from chessgrandmaster.relay_server import build_server

    root = tmp_path / "relay"
    server = build_server(root=root, host="127.0.0.1", port=0, admin_token="admin-token", worker_token="worker-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        admin = HTTPMailboxStorage(base_url=base, token="admin-token")
        worker = HTTPMailboxStorage(base_url=base, token="worker-token")
        current = {
            "schema_version": "cgm-molab-s3-current-1",
            "batch_id": "batch-a",
            "archive_path": "inbox/batches/batch-a.zip",
            "sha256": hashlib.sha256(b"batch").hexdigest(),
        }
        pointer = tmp_path / "current.json"; pointer.write_text(json.dumps(current), encoding="utf-8")
        archive = tmp_path / "batch.zip"; archive.write_bytes(b"batch")
        admin.upload("inbox/batches/batch-a.zip", archive)
        admin.upload("inbox/current.json", pointer)

        assert worker.read_bytes("inbox/current.json") == pointer.read_bytes()
        target = tmp_path / "got.zip"
        worker.download("inbox/batches/batch-a.zip", target)
        assert target.read_bytes() == b"batch"

        result = tmp_path / "job-result.json"; result.write_bytes(b"result")
        worker.upload("runtime/batch-a/result/results/0000/job-result.json", result)
        assert admin.read_bytes("runtime/batch-a/result/results/0000/job-result.json") == b"result"

        try:
            worker.read_bytes("inbox/batches/batch-b.zip")
        except PermissionError:
            pass
        else:
            raise AssertionError("worker must not read a different batch")
        try:
            worker.delete("inbox/current.json")
        except PermissionError:
            pass
        else:
            raise AssertionError("worker must not delete mailbox objects")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_relay_rejects_bad_upload_checksum(tmp_path: Path):
    from chessgrandmaster.coordinator.http_mailbox import HTTPMailboxStorage
    from chessgrandmaster.relay_server import build_server
    import urllib.error
    import urllib.request

    server = build_server(root=tmp_path / "relay", host="127.0.0.1", port=0, admin_token="admin-token", worker_token="worker-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/objects/inbox/current.json"
        req = urllib.request.Request(url, data=b"{}", method="PUT", headers={
            "Authorization": "Bearer admin-token",
            "Content-Length": "2",
            "X-CGM-SHA256": "0" * 64,
        })
        try:
            urllib.request.urlopen(req, timeout=2)
        except urllib.error.HTTPError as exc:
            assert exc.code == 422
        else:
            raise AssertionError("bad SHA must be rejected")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_molab_notebook_has_https_relay_one_click():
    text = (Path(__file__).resolve().parents[2] / "notebooks" / "molab_b1.py").read_text(encoding="utf-8")
    assert "Run next Oracle relay batch" in text
    assert "CGM_MOLAB_RELAY_URL" in text
    assert "CGM_MOLAB_RELAY_TOKEN" in text
    assert "run_molab_http_cycle.py" in text


def test_http_mailbox_upload_streams_without_path_read_bytes(tmp_path: Path, monkeypatch):
    from chessgrandmaster.coordinator.http_mailbox import HTTPMailboxStorage
    from chessgrandmaster.relay_server import build_server

    server = build_server(root=tmp_path / "relay", host="127.0.0.1", port=0, admin_token="admin-token", worker_token="worker-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    source = tmp_path / "large.bin"; source.write_bytes(b"x" * (2 * 1024 * 1024 + 17))
    original = Path.read_bytes
    def guarded(self):
        if self == source: raise AssertionError("upload must stream; Path.read_bytes() is forbidden")
        return original(self)
    monkeypatch.setattr(Path, "read_bytes", guarded)
    try:
        admin = HTTPMailboxStorage(base_url=f"http://127.0.0.1:{server.server_port}/v1", token="admin-token")
        admin.upload("runtime/batch-a/blob.bin", source)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
