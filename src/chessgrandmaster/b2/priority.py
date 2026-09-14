"""Deterministic, explainable tournament-priority scoring for B2."""

from dataclasses import dataclass


PLAYER_VIE = 100
EVENT_VIE = 60
CLASSICAL = 30
OTB = 20
GAME_VOLUME_100 = 10
GAME_VOLUME_500 = 10
SOURCE_PRIORITY_SCALE = 10

# These are Chess-Results domestic/club codes, not FIDE federation values.
_DOMESTIC_CODES = frozenset(
    {
        "HCM",
        "HNO",
        "DAN",
        "QDO",
        "DON",
        "BNI",
        "BLU",
    }
)


@dataclass(frozen=True)
class TournamentSignals:
    player_federations: tuple[str, ...] = ()
    event_country: str | None = None
    is_otb: bool | None = None
    time_control_class: str | None = None
    game_count: int = 0
    source_priority: int = 0
    # Aliases are deliberately supplied by configuration/callers; none are
    # inferred from domestic Chess-Results abbreviations.
    vietnamese_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriorityResult:
    score: int
    has_vietnamese_player: bool
    reasons: tuple[str, ...]


def _normalize_code(value: object) -> str:
    return str(value).strip().upper()


def score_tournament(signals: TournamentSignals) -> PriorityResult:
    """Score tournament signals with fixed weights and stable reason order."""
    if signals.game_count < 0:
        raise ValueError("game_count must not be negative")

    score = 0
    reasons: list[str] = []

    federations = {
        _normalize_code(value)
        for value in (signals.player_federations or ())
        if str(value).strip()
    }
    aliases = {
        _normalize_code(value)
        for value in (signals.vietnamese_aliases or ())
        if str(value).strip()
    }
    aliases -= _DOMESTIC_CODES

    has_vietnamese_player = "VIE" in federations
    if has_vietnamese_player:
        score += PLAYER_VIE
        reasons.append("player_federation:VIE")
    else:
        matched_aliases = sorted(federations & aliases)
        if matched_aliases:
            has_vietnamese_player = True
            score += PLAYER_VIE
            reasons.extend(
                f"player_federation_alias:{alias}" for alias in matched_aliases
            )

    event_country = (
        _normalize_code(signals.event_country)
        if signals.event_country is not None
        else None
    )
    if event_country == "VIE":
        score += EVENT_VIE
        reasons.append("event_country:VIE")

    time_control = (
        _normalize_code(signals.time_control_class)
        if signals.time_control_class is not None
        else None
    )
    if time_control in {"CLASSICAL", "STANDARD"}:
        score += CLASSICAL
        reasons.append(f"time_control:{time_control.casefold()}")

    if signals.is_otb is True or signals.is_otb == 1:
        score += OTB
        reasons.append("otb:true")

    if signals.game_count >= 100:
        score += GAME_VOLUME_100
        reasons.append("game_volume:100+")
    if signals.game_count >= 500:
        score += GAME_VOLUME_500
        reasons.append("game_volume:500+")

    source_priority = max(0, int(signals.source_priority))
    source_points = min(SOURCE_PRIORITY_SCALE, source_priority // 10)
    if source_points:
        score += source_points
        reasons.append(f"source_priority:+{source_points}")

    return PriorityResult(
        score=score,
        has_vietnamese_player=has_vietnamese_player,
        reasons=tuple(reasons),
    )
