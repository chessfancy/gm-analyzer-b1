"""Offline Vietnamese-player discovery hints backed by a pinned FIDE snapshot."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import unicodedata


_DATA_DIR = Path(__file__).with_name("data")


def _load_token_data() -> dict[str, frozenset[str]]:
    payload = json.loads(
        (_DATA_DIR / "vie_name_tokens.json").read_text(encoding="utf-8")
    )
    return {
        key: frozenset(str(value) for value in payload[key])
        for key in ("surnames", "name_tokens", "ambiguous_tokens")
    }


def _load_vie_fide_ids() -> frozenset[str]:
    return frozenset(
        line.strip()
        for line in (_DATA_DIR / "vie_fide_ids.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    )


_TOKEN_DATA = _load_token_data()
VIETNAMESE_SURNAMES = _TOKEN_DATA["surnames"]
VIETNAMESE_NAME_TOKENS = _TOKEN_DATA["name_tokens"]
AMBIGUOUS_TOKENS = _TOKEN_DATA["ambiguous_tokens"]
_VIE_FIDE_IDS = _load_vie_fide_ids()
_USEFUL_TOKENS = VIETNAMESE_SURNAMES | VIETNAMESE_NAME_TOKENS


@dataclass(frozen=True)
class VietnameseNameHint:
    """Deterministic, bounded evidence for discovery ranking only."""

    normalized_name: str
    matched_tokens: tuple[str, ...]
    matched_surnames: tuple[str, ...]
    strength: str


def normalize_player_name(name: str) -> tuple[str, ...]:
    """Normalize Unicode/diacritics and return punctuation-aware name tokens."""
    if not isinstance(name, str):
        raise TypeError("player name must be a string")
    text = name.replace("Đ", "D").replace("đ", "d")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    text = "".join(char if char.isalnum() else " " for char in text)
    return tuple(text.split())


def _distinct_matches(
    tokens: tuple[str, ...], vocabulary: frozenset[str]
) -> tuple[str, ...]:
    return tuple(dict.fromkeys(token for token in tokens if token in vocabulary))


def vietnamese_name_hint(name: str) -> VietnameseNameHint:
    """Return bounded strong/weak/none evidence without asserting nationality."""
    tokens = normalize_player_name(name)
    matched_tokens = _distinct_matches(tokens, _USEFUL_TOKENS)
    matched_surnames = _distinct_matches(tokens, VIETNAMESE_SURNAMES)
    if len(matched_tokens) >= 2:
        strength = "strong"
    elif matched_tokens:
        strength = "weak"
    else:
        strength = "none"
    return VietnameseNameHint(
        normalized_name=" ".join(tokens),
        matched_tokens=matched_tokens,
        matched_surnames=matched_surnames,
        strength=strength,
    )


def is_confirmed_vie_fide_id(fide_id: int | str) -> bool:
    """Return whether an ID is in the pinned official FED=VIE snapshot."""
    if isinstance(fide_id, bool):
        return False
    if isinstance(fide_id, int):
        candidate = str(fide_id)
    elif isinstance(fide_id, str):
        candidate = fide_id.strip()
    else:
        return False
    if not candidate.isdigit():
        return False
    return candidate in _VIE_FIDE_IDS
