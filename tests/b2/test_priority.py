from chessgrandmaster.b2.priority import TournamentSignals, score_tournament


def test_vie_player_is_highest_initial_signal():
    result = score_tournament(
        TournamentSignals(
            player_federations=("VIE", "SGP"),
            event_country=None,
            is_otb=True,
            time_control_class="classical",
            game_count=120,
            source_priority=50,
        )
    )

    assert result.score == 165
    assert result.has_vietnamese_player is True
    assert "player_federation:VIE" in result.reasons


def test_event_country_vie_is_recorded_without_inventing_player_identity():
    result = score_tournament(
        TournamentSignals(
            player_federations=(),
            event_country="VIE",
            is_otb=True,
            time_control_class="classical",
            game_count=200,
            source_priority=50,
        )
    )

    assert result.score == 125
    assert result.has_vietnamese_player is False
    assert "event_country:VIE" in result.reasons


def test_domestic_chess_results_codes_are_not_fide_vietnamese_evidence():
    result = score_tournament(
        TournamentSignals(
            player_federations=("HCM", "HNO", "DAN"),
            event_country="HCM",
            is_otb=False,
            time_control_class="rapid",
            game_count=0,
            source_priority=0,
        )
    )

    assert result.has_vietnamese_player is False
    assert result.score == 0
    assert all("VIE" not in reason for reason in result.reasons)


def test_explicit_aliases_are_opt_in_and_deterministic():
    signals = TournamentSignals(
        player_federations=(" vn ",),
        event_country=None,
        is_otb=False,
        time_control_class=None,
        game_count=0,
        source_priority=0,
        vietnamese_aliases=("VN",),
    )

    first = score_tournament(signals)
    second = score_tournament(signals)

    assert first == second
    assert first.score == 100
    assert first.has_vietnamese_player is True
    assert "player_federation_alias:VN" in first.reasons
