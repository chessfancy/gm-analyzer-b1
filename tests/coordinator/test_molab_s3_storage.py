from __future__ import annotations

from io import BytesIO
from pathlib import Path


class Body:
    def __init__(self, data: bytes): self.data = data
    def read(self): return self.data


class FakeS3Client:
    def __init__(self): self.objects = {}; self.deleted = []
    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = Path(filename).read_bytes()
    def download_file(self, bucket, key, filename):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        Path(filename).write_bytes(self.objects[(bucket, key)])
    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            error = RuntimeError("missing"); error.response = {"Error": {"Code": "NoSuchKey"}}; raise error
        return {"Body": Body(self.objects[(Bucket, Key)])}
    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None); self.deleted.append((Bucket, Key))
    def list_objects_v2(self, **kwargs):
        bucket, prefix = kwargs["Bucket"], kwargs.get("Prefix", "")
        return {"Contents": [{"Key": key} for (b, key) in self.objects if b == bucket and key.startswith(prefix)], "IsTruncated": False}


def test_s3_mailbox_prefix_round_trip(tmp_path: Path):
    from chessgrandmaster.coordinator.s3_mailbox import S3MailboxStorage
    client = FakeS3Client()
    storage = S3MailboxStorage(bucket="bucket", prefix="gm-analyzer/molab", client=client)
    source = tmp_path / "source.bin"; source.write_bytes(b"hello")

    storage.upload("inbox/test.bin", source)
    assert storage.read_bytes("inbox/test.bin") == b"hello"
    target = tmp_path / "nested" / "target.bin"
    storage.download("inbox/test.bin", target)
    assert target.read_bytes() == b"hello"
    assert storage.list("inbox/") == ["inbox/test.bin"]
    storage.delete("inbox/test.bin")
    assert storage.read_bytes("inbox/test.bin") is None
