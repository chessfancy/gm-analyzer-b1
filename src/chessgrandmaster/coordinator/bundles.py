"""Portable filesystem bundle validation and checksum helpers.

The coordinator intentionally treats a bundle as an ordinary directory.  A
provider can copy it by any means available, while these functions keep the
bytes and B2b identity independently verifiable on Oracle.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class BundleValidationError(ValueError):
    """Raised when a job/result bundle is missing, corrupt, or mismatched."""

    def __init__(
        self,
        message: str,
        *,
        bundle_path: Path | None = None,
        job_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.bundle_path = bundle_path
        self.job_id = job_id
        self.rejected_path: Path | None = None


@dataclass(frozen=True)
class ValidatedJobBundle:
    path: Path
    job_id: str
    config_hash: str
    input_identity: dict[str, Any]
    job_json: dict[str, Any]
    manifest_json: dict[str, Any]
    checksums: dict[str, str]


@dataclass(frozen=True)
class ValidatedResultBundle:
    path: Path
    job_id: str
    config_hash: str
    input_identity: dict[str, Any]
    result_json: dict[str, Any]
    checksums: dict[str, str]


_HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_JOB_REQUIRED = ("job.json", "manifest.json", "input.pgn")
_RESULT_REQUIRED = ("job-result.json", "analysis.sqlite", "Mistakes.pgn", "Blunders.pgn")


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON deterministically for metadata and checksum manifests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fail(message: str, path: Path, job_id: str | None = None) -> None:
    raise BundleValidationError(message, bundle_path=path, job_id=job_id)


def _load_json(path: Path, label: str, bundle_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"invalid {label}: {exc}", bundle_path)
    if not isinstance(value, dict):
        _fail(f"{label} must contain a JSON object", bundle_path)
    return value


def _regular_files(root: Path) -> dict[str, Path]:
    """Return safe relative file names and reject symlinks/path escapes."""

    if not root.exists() or not root.is_dir() or root.is_symlink():
        _fail("bundle path must be a real directory", root)
    root = root.resolve()
    files: dict[str, Path] = {}
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            _fail(f"symlinks are not allowed: {candidate}", root)
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root).as_posix()
        files[relative] = candidate
    return files


def _safe_checksum_name(name: object, bundle_path: Path) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        _fail(f"invalid checksum relative path: {name!r}", bundle_path)
    relative = Path(name)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        _fail(f"checksum path is not safely relative: {name!r}", bundle_path)
    return name


def _load_and_verify_checksums(
    root: Path,
    files: Mapping[str, Path],
    *,
    required: tuple[str, ...],
) -> dict[str, str]:
    checksum_path = root / "checksums.json"
    if not checksum_path.is_file() or checksum_path.is_symlink():
        _fail("checksums.json is required", root)
    checksums_value = _load_json(checksum_path, "checksums.json", root)
    if not checksums_value:
        _fail("checksums.json must not be empty", root)
    names = list(checksums_value)
    if names != sorted(names):
        _fail("checksums.json keys must be sorted", root)

    checksums: dict[str, str] = {}
    for name, digest in checksums_value.items():
        safe_name = _safe_checksum_name(name, root)
        if safe_name == "checksums.json":
            _fail("checksums.json cannot checksum itself", root)
        if not isinstance(digest, str) or not _HEX_SHA256.fullmatch(digest):
            _fail(f"invalid SHA-256 for {safe_name}", root)
        checksums[safe_name] = digest.lower()

    actual_names = set(files) - {"checksums.json"}
    declared_names = set(checksums)
    if actual_names != declared_names:
        missing = sorted(actual_names - declared_names)
        extra = sorted(declared_names - actual_names)
        _fail(f"checksum file set mismatch; missing={missing}, extra={extra}", root)
    missing_required = [name for name in required if name not in checksums]
    if missing_required:
        _fail(f"required files are not checksummed: {missing_required}", root)
    for name in sorted(checksums):
        actual = sha256_file(files[name])
        if actual != checksums[name]:
            _fail(f"checksum mismatch for {name}", root)
    return checksums


def _nested_value(payload: Mapping[str, Any], container: str, key: str) -> Any:
    nested = payload.get(container)
    if isinstance(nested, Mapping):
        return nested.get(key)
    return None


def _first_value(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return None


def _extract_job_id(payload: Mapping[str, Any]) -> str | None:
    value = _first_value(payload, "job_id", "jobId", "id")
    return str(value) if value is not None and str(value) else None


def _extract_config_hash(payload: Mapping[str, Any]) -> str | None:
    value = _first_value(payload, "config_hash", "configHash", "analysis_config_hash")
    if value is None:
        value = _nested_value(payload, "analysis", "config_hash")
    return str(value) if value is not None and str(value) else None


def _normalise_shard_index(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    text = str(value)
    try:
        return int(text)
    except ValueError:
        return text


def extract_input_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract common B2b input/shard identity aliases without rewriting JobSpec."""

    identity: dict[str, Any] = {}
    input_obj = payload.get("input")
    if not isinstance(input_obj, Mapping):
        input_obj = {}
    shard_obj = payload.get("shard")
    if not isinstance(shard_obj, Mapping):
        shard_obj = {}

    value = _first_value(
        input_obj,
        "sha256",
        "input_sha256",
        "sha",
        "hash",
    )
    if value is None:
        value = _first_value(shard_obj, "input_sha256", "sha256")
    if value is None:
        value = _first_value(payload, "input_sha256", "shard_input_sha256", "input_hash")
    if value is not None:
        identity["input_sha256"] = str(value).lower()

    value = _first_value(input_obj, "key", "object_key", "path")
    if value is None:
        value = _first_value(shard_obj, "input_key", "key", "object_key")
    if value is None:
        value = _first_value(payload, "input_key", "shard_key", "object_key")
    if value is not None:
        identity["input_key"] = str(value)

    value = _first_value(payload, "shard_index")
    if value is None:
        value = _first_value(input_obj, "shard_index", "index")
    if value is None:
        value = _first_value(shard_obj, "index", "shard_index")
    if value is not None:
        identity["shard_index"] = _normalise_shard_index(value)

    value = _first_value(payload, "shard_id", "shard_identity")
    if value is None:
        value = _first_value(shard_obj, "id", "identity")
    if value is not None:
        identity["shard_id"] = str(value)

    value = _first_value(input_obj, "canonical_game_fingerprints", "fingerprints")
    if value is None:
        value = _first_value(payload, "canonical_game_fingerprints", "fingerprints")
    if value is not None:
        if not isinstance(value, (list, tuple)):
            raise BundleValidationError("canonical game fingerprints must be a list")
        identity["canonical_game_fingerprints"] = tuple(str(item) for item in value)

    value = _first_value(payload, "tournament_id")
    if value is not None:
        identity["tournament_id"] = str(value)
    value = _first_value(payload, "tournament_revision", "revision")
    if value is not None:
        identity["tournament_revision"] = _normalise_shard_index(value)
    return identity


def _validate_common_json(
    root: Path,
    *,
    required: tuple[str, ...],
) -> tuple[dict[str, Path], dict[str, str]]:
    files = _regular_files(root)
    for name in required:
        if name not in files:
            _fail(f"required bundle file is missing: {name}", root)
    checksums = _load_and_verify_checksums(root, files, required=required)
    return files, checksums


def validate_job_bundle(path: str | Path) -> ValidatedJobBundle:
    """Validate a portable B2b job bundle before queue registration/export."""

    root = Path(path)
    files, checksums = _validate_common_json(root, required=_JOB_REQUIRED)
    job = _load_json(files["job.json"], "job.json", root)
    manifest = _load_json(files["manifest.json"], "manifest.json", root)
    job_id = _extract_job_id(job)
    if not job_id:
        _fail("job.json must contain a non-empty job_id", root)
    manifest_job_id_value = _first_value(manifest, "job_id", "jobId")
    manifest_job_id = str(manifest_job_id_value) if manifest_job_id_value is not None else None
    if manifest_job_id is not None and manifest_job_id != job_id:
        _fail("manifest job_id does not match job.json", root, job_id)
    config_hash = _extract_config_hash(job)
    if not config_hash:
        _fail("job.json must contain a non-empty config_hash", root, job_id)

    input_sha256 = sha256_file(files["input.pgn"])
    identity = extract_input_identity(job)
    declared_input_sha = identity.get("input_sha256")
    if declared_input_sha is not None and declared_input_sha != input_sha256:
        _fail("job input sha256 does not match input.pgn", root, job_id)
    identity["input_sha256"] = input_sha256
    return ValidatedJobBundle(
        path=root.resolve(),
        job_id=job_id,
        config_hash=config_hash,
        input_identity=identity,
        job_json=job,
        manifest_json=manifest,
        checksums=checksums,
    )


def _expected_parts(expected_job: ValidatedJobBundle | Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if isinstance(expected_job, ValidatedJobBundle):
        return expected_job.job_id, expected_job.config_hash, expected_job.input_identity
    job_id = _extract_job_id(expected_job)
    config_hash = _extract_config_hash(expected_job)
    if not job_id or not config_hash:
        raise BundleValidationError("expected job metadata is incomplete")
    identity = extract_input_identity(expected_job)
    return job_id, config_hash, identity


def validate_result_bundle(
    path: str | Path,
    *,
    expected_job: ValidatedJobBundle | Mapping[str, Any] | None = None,
) -> ValidatedResultBundle:
    """Validate outputs and, when supplied, match them to a registered job."""

    root = Path(path)
    files, checksums = _validate_common_json(root, required=_RESULT_REQUIRED)
    raw_uci = [
        name
        for name in files
        if name != "job-result.json"
        and (
            name == "raw-uci"
            or name.startswith("raw-uci/")
            or name.startswith("raw_uci/")
            or Path(name).name.lower().startswith("raw-uci")
            or Path(name).name.lower().startswith("raw_uci")
        )
    ]
    if not raw_uci:
        _fail("result bundle must contain raw-uci archive/files", root)
    result = _load_json(files["job-result.json"], "job-result.json", root)
    job_id = _extract_job_id(result)
    if not job_id:
        _fail("job-result.json must contain a non-empty job_id", root)
    config_hash = _extract_config_hash(result)
    if not config_hash:
        _fail("job-result.json must contain a non-empty config_hash", root, job_id)
    identity = extract_input_identity(result)
    identity_fields = {"input_sha256", "input_key", "shard_index", "shard_id", "canonical_game_fingerprints"}
    if not identity_fields.intersection(identity):
        _fail("job-result.json must carry input/shard identity", root, job_id)

    if expected_job is not None:
        expected_id, expected_config, expected_identity = _expected_parts(expected_job)
        if job_id != expected_id:
            _fail(f"result job_id {job_id!r} does not match {expected_id!r}", root, job_id)
        if config_hash != expected_config:
            _fail("result config_hash does not match registered job", root, job_id)
        for field in identity_fields:
            if field in identity and field in expected_identity:
                if identity[field] != expected_identity[field]:
                    _fail(f"result {field} does not match registered job", root, job_id)
    return ValidatedResultBundle(
        path=root.resolve(),
        job_id=job_id,
        config_hash=config_hash,
        input_identity=identity,
        result_json=result,
        checksums=checksums,
    )


def _copy_bundle_directory(source: Path, destination: Path) -> Path:
    source = source.resolve()
    destination = Path(destination).resolve()
    if source == destination:
        return destination
    if destination.exists() and not destination.is_dir():
        raise BundleValidationError(f"destination is not a directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=str(destination.parent)))
    staged = staging / "bundle"
    try:
        shutil.copytree(source, staged)
        if destination.exists():
            shutil.rmtree(destination)
        staged.replace(destination)
        return destination
    finally:
        shutil.rmtree(staging, ignore_errors=True)


class FilesystemBundleTransport:
    """Provider-neutral filesystem transport for independently verified bundles."""

    def export_job(self, bundle_path: str | Path, destination: str | Path) -> Path:
        validated = validate_job_bundle(bundle_path)
        return _copy_bundle_directory(validated.path, Path(destination))

    def import_result(
        self,
        result_path: str | Path,
        *,
        expected_job: ValidatedJobBundle | Mapping[str, Any] | None = None,
    ) -> ValidatedResultBundle:
        return validate_result_bundle(result_path, expected_job=expected_job)



def compute_checksums(path: str | Path) -> dict[str, str]:
    """Compute sorted SHA-256 entries for every file except checksums.json."""

    root = Path(path)
    files = _regular_files(root)
    return {name: sha256_file(files[name]) for name in sorted(files) if name != "checksums.json"}


def write_checksums(path: str | Path) -> dict[str, str]:
    """Write the deterministic sorted checksum manifest and return its mapping."""

    root = Path(path)
    checksums = compute_checksums(root)
    (root / "checksums.json").write_bytes(canonical_json_bytes(checksums))
    return checksums


__all__ = [
    "BundleValidationError",
    "FilesystemBundleTransport",
    "ValidatedJobBundle",
    "ValidatedResultBundle",
    "canonical_json_bytes",
    "compute_checksums",
    "extract_input_identity",
    "sha256_bytes",
    "sha256_file",
    "validate_job_bundle",
    "validate_result_bundle",
    "write_checksums",
]
