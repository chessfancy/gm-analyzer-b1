"""Provider-neutral source adapter contracts for B2 acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SourceRef:
    """Stable provider-specific identity plus the supplied source URL."""

    provider: str
    external_id: str
    source_url: str


@dataclass(frozen=True)
class SourceDescriptor:
    """Metadata discovered from one source page."""

    ref: SourceRef
    title: str | None
    pgn_url: str | None
    event_country_hint: str | None
    time_control_hint: str | None
    is_otb_hint: bool | None


class SourceAdapter(Protocol):
    """Common interface implemented by B2 source providers."""

    def discover(self, query: str) -> list[SourceRef]:
        ...

    def describe(self, ref: SourceRef) -> SourceDescriptor:
        ...

    def download_pgn(self, ref: SourceRef, destination: Path) -> Path:
        ...


__all__ = ["SourceAdapter", "SourceDescriptor", "SourceRef"]
