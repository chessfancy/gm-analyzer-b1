"""Optional S3-compatible ObjectStore backend for B2b transport."""

from __future__ import annotations

from pathlib import Path
import os
import tempfile
from typing import Any

from .storage import LocalObjectStore, ObjectStat, sha256_file


class S3ObjectStore:
    """Store immutable logical keys in an injected or lazily-created S3 client."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        client: Any | None = None,
        *,
        endpoint_url: str | None = None,
    ):
        if not isinstance(bucket, str) or not bucket:
            raise ValueError("S3 bucket must not be empty")
        self.bucket = bucket
        if prefix.startswith("/"):
            raise ValueError("S3 prefix must be relative")
        normalized_prefix = prefix[:-1] if prefix.endswith("/") else prefix
        if normalized_prefix:
            LocalObjectStore._validate_key(normalized_prefix, prefix=False)
            normalized_prefix += "/"
        self.prefix = normalized_prefix
        self._client = client
        self.endpoint_url = endpoint_url

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "S3 storage requires the optional 's3' dependency (boto3)"
                ) from exc
            kwargs = {}
            if self.endpoint_url is not None:
                kwargs["endpoint_url"] = self.endpoint_url
            self._client = boto3.client("s3", **kwargs)
        return self._client

    @staticmethod
    def _validate_key(key: str, *, prefix: bool = False) -> str:
        return LocalObjectStore._validate_key(key, prefix=prefix)

    def _remote_key(self, key: str) -> str:
        key = self._validate_key(key)
        return f"{self.prefix}{key}"

    def _remote_prefix(self, prefix: str) -> str:
        prefix = self._validate_key(prefix, prefix=True)
        if prefix:
            return f"{self.prefix}{prefix}"
        return self.prefix

    @staticmethod
    def _not_found(error: BaseException) -> bool:
        if isinstance(error, (FileNotFoundError, KeyError)):
            return True
        response = getattr(error, "response", None)
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))
            return code in {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}
        return False

    def _head(self, key: str) -> dict[str, Any]:
        return self.client.head_object(Bucket=self.bucket, Key=self._remote_key(key))

    @staticmethod
    def _metadata_sha(head: dict[str, Any]) -> str | None:
        metadata = head.get("Metadata") or {}
        normalized = {str(key).casefold(): value for key, value in metadata.items()}
        value = normalized.get("cgm-sha256")
        return None if value is None else str(value)

    def put_file(self, key: str, path: Path) -> ObjectStat:
        key = self._validate_key(key)
        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(source)
        expected_size = source.stat().st_size
        expected_sha256 = sha256_file(source)
        try:
            existing = self._head(key)
        except Exception as exc:
            if not self._not_found(exc):
                raise
            existing = None
        if existing is not None:
            existing_sha256 = self._metadata_sha(existing)
            if (
                existing_sha256 == expected_sha256
                and int(existing.get("ContentLength", expected_size)) == expected_size
            ):
                return ObjectStat(key, expected_size, expected_sha256)
            raise ValueError(f"immutable object key already contains different bytes: {key}")

        self.client.upload_file(
            str(source),
            self.bucket,
            self._remote_key(key),
            ExtraArgs={"Metadata": {"cgm-sha256": expected_sha256}},
        )
        stored = self.stat(key)
        if stored.size != expected_size or stored.sha256 != expected_sha256:
            raise ValueError(f"uploaded object failed checksum verification: {key}")
        return stored

    def get_file(self, key: str, destination: Path) -> ObjectStat:
        key = self._validate_key(key)
        head = self._head(key)
        expected_sha256 = self._metadata_sha(head)
        if expected_sha256 is None:
            raise ValueError(f"S3 object is missing cgm-sha256 metadata: {key}")
        expected_size = int(head.get("ContentLength", -1))
        if expected_size < 0:
            raise ValueError(f"S3 object is missing ContentLength: {key}")

        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        try:
            self.client.download_file(
                self.bucket,
                self._remote_key(key),
                str(temporary),
            )
            actual_size = temporary.stat().st_size
            actual_sha256 = sha256_file(temporary)
            if (actual_size, actual_sha256) != (expected_size, expected_sha256):
                raise ValueError(f"downloaded object failed checksum verification: {key}")
            os.replace(temporary, destination)
            return ObjectStat(key, actual_size, actual_sha256)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def exists(self, key: str) -> bool:
        key = self._validate_key(key)
        try:
            self._head(key)
        except Exception as exc:
            if self._not_found(exc):
                return False
            raise
        return True

    def stat(self, key: str) -> ObjectStat:
        key = self._validate_key(key)
        head = self._head(key)
        sha256 = self._metadata_sha(head)
        if sha256 is None:
            raise ValueError(f"S3 object is missing cgm-sha256 metadata: {key}")
        size = int(head.get("ContentLength", -1))
        if size < 0:
            raise ValueError(f"S3 object is missing ContentLength: {key}")
        return ObjectStat(key, size, sha256)

    def list(self, prefix: str) -> list[str]:
        prefix = self._validate_key(prefix, prefix=True)
        remote_prefix = self._remote_prefix(prefix)
        logical_keys: list[str] = []
        request: dict[str, Any] = {"Bucket": self.bucket, "Prefix": remote_prefix}
        while True:
            response = self.client.list_objects_v2(**request)
            for item in response.get("Contents", ()):
                remote_key = str(item["Key"])
                if not remote_key.startswith(self.prefix):
                    continue
                logical_key = remote_key[len(self.prefix) :]
                if logical_key.startswith(prefix):
                    self._validate_key(logical_key)
                    logical_keys.append(logical_key)
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                raise RuntimeError("S3 list response is truncated without a continuation token")
            request["ContinuationToken"] = token
        return sorted(set(logical_keys))
