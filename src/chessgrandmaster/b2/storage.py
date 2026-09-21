"""Immutable local implementation of the B2 object-store contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import tempfile
from threading import RLock
from typing import Protocol


_COPY_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ObjectStat:
    key: str
    size: int
    sha256: str


class ObjectStore(Protocol):
    def put_file(self, key: str, path: Path) -> ObjectStat:
        ...

    def get_file(self, key: str, destination: Path) -> ObjectStat:
        ...

    def exists(self, key: str) -> bool:
        ...

    def stat(self, key: str) -> ObjectStat:
        ...

    def list(self, prefix: str) -> list[str]:
        ...


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0

    with path.open("rb") as source:
        for block in iter(lambda: source.read(_COPY_CHUNK_SIZE), b""):
            digest.update(block)
            size += len(block)

    return size, digest.hexdigest()


def sha256_file(path: Path) -> str:
    """Return the stable SHA256 digest of a regular file."""
    return _file_digest(Path(path))[1]


def _copy_and_digest(source: Path, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0

    with source.open("rb") as source_file, destination.open("wb") as destination_file:
        for block in iter(lambda: source_file.read(_COPY_CHUNK_SIZE), b""):
            destination_file.write(block)
            digest.update(block)
            size += len(block)
        destination_file.flush()
        os.fsync(destination_file.fileno())

    return size, digest.hexdigest()


class LocalObjectStore:
    """Store immutable objects below one local filesystem root."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)
        self._lock = RLock()

    @staticmethod
    def _validate_key(key: str, *, prefix: bool = False) -> str:
        if not isinstance(key, str):
            raise TypeError("object key must be a string")
        if not key and not prefix:
            raise ValueError("object key must not be empty")
        if "\x00" in key:
            raise ValueError("object key must not contain NUL")
        if "\\" in key:
            raise ValueError("object keys must use forward slashes")
        if key.startswith("/") or PurePosixPath(key).is_absolute():
            raise ValueError("absolute object keys are not allowed")

        windows_key = PureWindowsPath(key)
        if windows_key.drive or windows_key.root:
            raise ValueError("Windows absolute or drive-qualified keys are not allowed")

        parts = key.split("/")
        if any(part in {".", ".."} for part in parts):
            raise ValueError("path traversal is not allowed in object keys")
        if prefix:
            if any(part == "" for part in parts[:-1]):
                raise ValueError("empty object-key path segments are not allowed")
        elif any(part == "" for part in parts):
            raise ValueError("empty object-key path segments are not allowed")

        return key

    def _path_for_key(self, key: str) -> Path:
        key = self._validate_key(key)
        path = self.root.joinpath(*key.split("/"))
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("object key escapes the storage root") from exc
        return path

    @staticmethod
    def _occupied(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    def put_file(self, key: str, path: Path) -> ObjectStat:
        """Copy a file to an immutable key, reusing an exact existing object."""
        destination = self._path_for_key(key)
        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(source)

        with self._lock:
            expected_size, expected_sha256 = _file_digest(source)

            if self._occupied(destination):
                if not destination.is_file():
                    raise ValueError(f"immutable object key is not a file: {key}")
                existing_size, existing_sha256 = _file_digest(destination)
                if (
                    existing_size == expected_size
                    and existing_sha256 == expected_sha256
                ):
                    return ObjectStat(key, existing_size, existing_sha256)
                raise ValueError(
                    f"immutable object key already contains different bytes: {key}"
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                temporary_fd, temporary_name = tempfile.mkstemp(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".part",
                )
                os.close(temporary_fd)
                temporary_path = Path(temporary_name)
                actual_size, actual_sha256 = _copy_and_digest(
                    source,
                    temporary_path,
                )
                if (actual_size, actual_sha256) != (
                    expected_size,
                    expected_sha256,
                ):
                    raise OSError("source changed while storing immutable object")

                if self._occupied(destination):
                    if not destination.is_file():
                        raise ValueError(f"immutable object key is not a file: {key}")
                    existing_size, existing_sha256 = _file_digest(destination)
                    if (
                        existing_size == expected_size
                        and existing_sha256 == expected_sha256
                    ):
                        return ObjectStat(key, existing_size, existing_sha256)
                    raise ValueError(
                        f"immutable object key already contains different bytes: {key}"
                    )

                # The existence check is deliberately immediately before the
                # atomic install; an existing immutable key is never replaced.
                os.replace(temporary_path, destination)
                temporary_path = None

                stored_size, stored_sha256 = _file_digest(destination)
                if (stored_size, stored_sha256) != (
                    expected_size,
                    expected_sha256,
                ):
                    raise OSError("stored object failed checksum verification")
                return ObjectStat(key, stored_size, stored_sha256)
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass

    def get_file(self, key: str, destination: Path) -> ObjectStat:
        """Copy an object to a destination and verify the copied bytes."""
        source = self._path_for_key(key)
        if not source.is_file():
            raise FileNotFoundError(f"object not found: {key}")

        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_size, expected_sha256 = _file_digest(source)
        temporary_path: Path | None = None
        try:
            temporary_fd, temporary_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".part",
            )
            os.close(temporary_fd)
            temporary_path = Path(temporary_name)
            actual_size, actual_sha256 = _copy_and_digest(
                source,
                temporary_path,
            )
            if (actual_size, actual_sha256) != (
                expected_size,
                expected_sha256,
            ):
                raise OSError("object changed while restoring file")

            os.replace(temporary_path, destination)
            temporary_path = None

            restored_size, restored_sha256 = _file_digest(destination)
            if (restored_size, restored_sha256) != (
                expected_size,
                expected_sha256,
            ):
                raise OSError("restored file failed checksum verification")
            return ObjectStat(key, restored_size, restored_sha256)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def exists(self, key: str) -> bool:
        return self._path_for_key(key).is_file()

    def stat(self, key: str) -> ObjectStat:
        path = self._path_for_key(key)
        if not path.is_file():
            raise FileNotFoundError(f"object not found: {key}")
        size, sha256 = _file_digest(path)
        return ObjectStat(key, size, sha256)

    def list(self, prefix: str) -> list[str]:
        prefix = self._validate_key(prefix, prefix=True)
        if not self.root.exists():
            return []

        keys: list[str] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            self._path_for_key(key)
            if key.startswith(prefix):
                keys.append(key)
        return sorted(keys)
