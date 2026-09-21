from pathlib import Path

import pytest

from chessgrandmaster.b2.s3_storage import S3ObjectStore


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.uploads = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        metadata = dict((ExtraArgs or {}).get("Metadata", {}))
        self.objects[(bucket, key)] = (Path(filename).read_bytes(), metadata)
        self.uploads.append((bucket, key))

    def head_object(self, Bucket, Key):
        try:
            content, metadata = self.objects[(Bucket, Key)]
        except KeyError as exc:
            error = RuntimeError("not found")
            error.response = {"Error": {"Code": "404"}}
            raise error from exc
        return {"ContentLength": len(content), "Metadata": dict(metadata)}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)][0])

    def list_objects_v2(self, Bucket, Prefix, **kwargs):
        keys = sorted(
            key
            for (bucket, key) in self.objects
            if bucket == Bucket and key.startswith(Prefix)
        )
        page_size = 1
        start = 0
        token = kwargs.get("ContinuationToken")
        if token is not None:
            start = int(token)
        page = keys[start : start + page_size]
        response = {"Contents": [{"Key": key} for key in page]}
        if start + page_size < len(keys):
            response["IsTruncated"] = True
            response["NextContinuationToken"] = str(start + page_size)
        else:
            response["IsTruncated"] = False
        return response


def test_s3_store_uses_logical_prefix_and_supports_round_trip_and_pagination(tmp_path):
    client = FakeS3Client()
    store = S3ObjectStore("bucket", prefix="jobs", client=client)
    source = tmp_path / "input.pgn"
    source.write_bytes(b"portable input")

    stat = store.put_file("a/input.pgn", source)
    assert stat.key == "a/input.pgn"
    assert stat.size == len(b"portable input")
    assert store.stat("a/input.pgn") == stat
    assert store.exists("a/input.pgn")

    restored = tmp_path / "restored.pgn"
    assert store.get_file("a/input.pgn", restored) == stat
    assert restored.read_bytes() == source.read_bytes()

    second = tmp_path / "second"
    second.write_bytes(b"second")
    store.put_file("b/second", second)
    assert store.list("") == ["a/input.pgn", "b/second"]
    assert client.uploads == [("bucket", "jobs/a/input.pgn"), ("bucket", "jobs/b/second")]


def test_s3_store_reuses_same_sha_and_rejects_immutable_collision(tmp_path):
    client = FakeS3Client()
    store = S3ObjectStore("bucket", client=client)
    first = tmp_path / "first"
    same = tmp_path / "same"
    different = tmp_path / "different"
    first.write_bytes(b"same")
    same.write_bytes(b"same")
    different.write_bytes(b"different")

    original = store.put_file("immutable", first)
    assert store.put_file("immutable", same) == original
    with pytest.raises(ValueError, match="immutable"):
        store.put_file("immutable", different)
    assert len(client.uploads) == 1


def test_s3_store_does_not_import_boto3_for_injected_clients(tmp_path):
    client = FakeS3Client()
    source = tmp_path / "source"
    source.write_bytes(b"data")

    S3ObjectStore("bucket", client=client).put_file("source", source)
