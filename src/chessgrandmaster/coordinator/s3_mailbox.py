"""Mutable S3-compatible mailbox adapter for coordinator worker handoffs."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import tempfile

from chessgrandmaster.b2.storage import LocalObjectStore


class S3MailboxStorage:
    def __init__(self, *, bucket: str, prefix: str = "", client: Any | None = None,
                 endpoint_url: str | None = None, access_key: str | None = None,
                 secret_key: str | None = None) -> None:
        if not bucket:
            raise ValueError("S3 bucket must not be empty")
        self.bucket = bucket
        normalized = prefix.strip("/")
        if normalized:
            LocalObjectStore._validate_key(normalized, prefix=False)
            normalized += "/"
        self.prefix = normalized
        self._client = client
        self.endpoint_url = endpoint_url
        self.access_key = access_key
        self.secret_key = secret_key

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise RuntimeError("Molab S3 mailbox requires boto3") from exc
            kwargs: dict[str, object] = {
                "config": Config(signature_version="s3v4", s3={"addressing_style": "path"})
            }
            if self.endpoint_url:
                kwargs["endpoint_url"] = self.endpoint_url
            if self.access_key:
                kwargs["aws_access_key_id"] = self.access_key
            if self.secret_key:
                kwargs["aws_secret_access_key"] = self.secret_key
            self._client = boto3.client("s3", **kwargs)
        return self._client

    @staticmethod
    def _validate(key: str, *, prefix: bool = False) -> str:
        return LocalObjectStore._validate_key(key, prefix=prefix)

    def _remote(self, key: str) -> str:
        return f"{self.prefix}{self._validate(key)}"

    def _remote_prefix(self, prefix: str) -> str:
        return f"{self.prefix}{self._validate(prefix, prefix=True)}"

    @staticmethod
    def _not_found(exc: BaseException) -> bool:
        if isinstance(exc, (FileNotFoundError, KeyError)):
            return True
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))
            return code in {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}
        return False

    def read_bytes(self, path: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._remote(path))
        except Exception as exc:
            if self._not_found(exc):
                return None
            raise
        body = response["Body"]
        data = body.read()
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError("S3 mailbox returned non-byte object body")
        return bytes(data)

    def upload(self, path: str, source: Path) -> None:
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(source)
        self.client.upload_file(str(source), self.bucket, self._remote(path))

    def download(self, path: str, destination: Path) -> None:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.", suffix=".part")
        os.close(fd)
        temp = Path(name)
        try:
            self.client.download_file(self.bucket, self._remote(path), str(temp))
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)

    def delete(self, path: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._remote(path))

    def list(self, prefix: str = "") -> list[str]:
        remote_prefix = self._remote_prefix(prefix)
        request: dict[str, object] = {"Bucket": self.bucket, "Prefix": remote_prefix}
        result: list[str] = []
        while True:
            response = self.client.list_objects_v2(**request)
            for item in response.get("Contents", ()):
                remote = str(item["Key"])
                if remote.startswith(self.prefix):
                    logical = remote[len(self.prefix):]
                    self._validate(logical)
                    result.append(logical)
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                raise RuntimeError("S3 mailbox listing truncated without continuation token")
            request["ContinuationToken"] = token
        return sorted(set(result))
