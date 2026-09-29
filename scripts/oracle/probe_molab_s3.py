#!/usr/bin/env python3
from __future__ import annotations
import hashlib, json, os, tempfile, uuid
from pathlib import Path

from chessgrandmaster.coordinator.s3_mailbox import S3MailboxStorage


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def main() -> int:
    storage = S3MailboxStorage(
        endpoint_url=required("CGM_MOLAB_S3_ENDPOINT"), bucket=required("CGM_MOLAB_S3_BUCKET"),
        prefix=os.environ.get("CGM_MOLAB_S3_PREFIX", "gm-analyzer/molab"),
        access_key=required("CGM_MOLAB_S3_ACCESS_KEY"), secret_key=required("CGM_MOLAB_S3_SECRET_KEY"),
    )
    payload = f"cgm-molab-s3-probe:{uuid.uuid4().hex}\n".encode()
    key = f"probe/{uuid.uuid4().hex}.txt"
    with tempfile.TemporaryDirectory() as temp_dir:
        source = Path(temp_dir) / "source.txt"; source.write_bytes(payload)
        target = Path(temp_dir) / "target.txt"
        storage.upload(key, source)
        direct = storage.read_bytes(key)
        storage.download(key, target)
        if direct != payload or target.read_bytes() != payload:
            raise RuntimeError("S3 probe round-trip bytes mismatch")
        sha = hashlib.sha256(payload).hexdigest()
        storage.delete(key)
        if storage.read_bytes(key) is not None:
            raise RuntimeError("S3 probe delete verification failed")
    print(json.dumps({
        "ok": True, "endpoint": required("CGM_MOLAB_S3_ENDPOINT"),
        "bucket": required("CGM_MOLAB_S3_BUCKET"),
        "prefix": os.environ.get("CGM_MOLAB_S3_PREFIX", "gm-analyzer/molab"),
        "sha256": sha, "deleted": True,
    }, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
