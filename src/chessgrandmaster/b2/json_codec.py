"""Deterministic JSON serialization and hashing helpers for B2."""

import hashlib
import json


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a JSON-compatible value using the B2 canonical form."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(value: object) -> str:
    """Return the SHA256 digest of a value's canonical JSON bytes."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()
