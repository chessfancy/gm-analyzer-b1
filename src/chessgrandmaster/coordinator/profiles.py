"""Provider-neutral runtime profiles kept outside B2b JobSpec/config hashes."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ProviderProfile:
    """Conservative runtime settings for a worker platform.

    These values describe execution capacity only.  They are deliberately not
    serialized into ``job.json`` or used to calculate the B2b ``config_hash``.
    """

    name: str
    workers: int
    threads: int
    hash_mb: int
    depth: int = 19
    role: str = "bulk"
    max_concurrent_jobs: int | None = None

    def __post_init__(self) -> None:
        if self.workers <= 0 or self.threads <= 0 or self.hash_mb <= 0:
            raise ValueError("worker, thread, and hash values must be positive")
        if self.depth <= 0:
            raise ValueError("depth must be positive")
        if self.max_concurrent_jobs is not None and self.max_concurrent_jobs <= 0:
            raise ValueError("max_concurrent_jobs must be positive")

    @property
    def hash(self) -> int:
        """Compatibility alias for callers that call Stockfish Hash ``hash``."""

        return self.hash_mb

    @property
    def one_job_at_a_time(self) -> bool:
        return self.max_concurrent_jobs == 1

    @property
    def is_bulk(self) -> bool:
        return self.role == "bulk"

    def to_dict(self) -> dict[str, object]:
        """Return runtime metadata without implying JobSpec membership."""

        return {
            "name": self.name,
            "workers": self.workers,
            "threads": self.threads,
            "hash_mb": self.hash_mb,
            "depth": self.depth,
            "role": self.role,
            "max_concurrent_jobs": self.max_concurrent_jobs,
        }


_CANONICAL_PROFILES = {
    "kaggle": ProviderProfile(
        name="kaggle", workers=4, threads=1, hash_mb=1536, depth=19
    ),
    "deepnote": ProviderProfile(
        name="deepnote", workers=2, threads=1, hash_mb=768, depth=19
    ),
    "molab-marimo": ProviderProfile(
        name="molab-marimo", workers=4, threads=1, hash_mb=1536, depth=19
    ),
    "codespaces": ProviderProfile(
        name="codespaces", workers=2, threads=1, hash_mb=1024, depth=19
    ),
    "oracle-urgent": ProviderProfile(
        name="oracle-urgent",
        workers=2,
        threads=1,
        hash_mb=1536,
        depth=19,
        role="secondary-urgent",
        max_concurrent_jobs=1,
    ),
}

# The aliases make manual dispatch less error-prone while preserving one
# canonical profile object and one provider value in attempt metadata.
_PROFILE_ALIASES = {
    "molab": "molab-marimo",
    "marimo": "molab-marimo",
    "molab/marimo": "molab-marimo",
    "github-codespaces": "codespaces",
    "oracle": "oracle-urgent",
}

PROFILE_REGISTRY: Mapping[str, ProviderProfile] = MappingProxyType(
    {**_CANONICAL_PROFILES, **{alias: _CANONICAL_PROFILES[name] for alias, name in _PROFILE_ALIASES.items()}}
)
PROVIDER_PROFILES = PROFILE_REGISTRY
RUNTIME_PROFILES = PROFILE_REGISTRY


def get_provider_profile(name: str | ProviderProfile) -> ProviderProfile:
    """Resolve a supported provider name to its immutable runtime profile."""

    if isinstance(name, ProviderProfile):
        return name
    normalized = str(name).strip().lower()
    try:
        return PROFILE_REGISTRY[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(_CANONICAL_PROFILES))
        raise ValueError(f"unknown provider profile {name!r}; supported: {supported}") from exc


provider_profile = get_provider_profile

__all__ = [
    "PROFILE_REGISTRY",
    "PROVIDER_PROFILES",
    "RUNTIME_PROFILES",
    "ProviderProfile",
    "get_provider_profile",
    "provider_profile",
]
