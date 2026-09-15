from __future__ import annotations

import pytest

from chessgrandmaster.discovery.name_filter import (
    AMBIGUOUS_TOKENS,
    VIETNAMESE_NAME_TOKENS,
    VIETNAMESE_SURNAMES,
    is_confirmed_vie_fide_id,
    normalize_player_name,
    vietnamese_name_hint,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Nguyễn, Hoàng Minh", ("nguyen", "hoang", "minh")),
        ("Đặng Tiến Phong", ("dang", "tien", "phong")),
        ("NGUYEN, Thai Dai Van", ("nguyen", "thai", "dai", "van")),
        ("", ()),
        ("   !!! ", ()),
    ],
)
def test_normalize_player_name(raw, expected):
    assert normalize_player_name(raw) == expected


@pytest.mark.parametrize(
    "name",
    [
        "Nguyen Minh",
        "Nguyen Hoang Minh",
        "Dang Tien Phong",
        "Tran Thanh Bao",
        "Nguyen Thai Dai Van",
        "Nguyen Hong",
        "Tran Mai",
        "Dang Van Tien",
    ],
)
def test_two_distinct_useful_tokens_are_strong(name):
    assert vietnamese_name_hint(name).strength == "strong"


@pytest.mark.parametrize(
    "name",
    [
        "Minh Smith",
        "Hong Lee",
        "Mai Chen",
        "Nguyen",
        "Le Wang",
        "Van der Meer",
    ],
)
def test_one_useful_token_is_weak(name):
    assert vietnamese_name_hint(name).strength == "weak"


@pytest.mark.parametrize(
    "name",
    [
        "Magnus Carlsen",
        "Fabiano Caruana",
        "Nodirbek Abdusattorov",
        "Hikaru Nakamura",
        "",
    ],
)
def test_no_useful_tokens_are_none(name):
    assert vietnamese_name_hint(name).strength == "none"


def test_duplicate_token_does_not_reach_strong_threshold():
    hint = vietnamese_name_hint("Nguyen Nguyen")

    assert hint.strength == "weak"
    assert hint.matched_tokens == ("nguyen",)


def test_hint_preserves_normalized_name_and_distinct_matches():
    hint = vietnamese_name_hint("NGUYEN, Thai Dai Van")

    assert hint.normalized_name == "nguyen thai dai van"
    assert hint.matched_tokens == ("nguyen", "thai", "dai", "van")
    assert hint.matched_surnames == ("nguyen",)
    assert hint.strength == "strong"


def test_static_dictionaries_include_core_tokens():
    assert {"nguyen", "tran", "le", "pham", "dang"} <= VIETNAMESE_SURNAMES
    assert {"minh", "thanh", "tien", "phong", "dai", "van"} <= (
        VIETNAMESE_NAME_TOKENS
    )
    assert AMBIGUOUS_TOKENS == frozenset(
        {"hong", "mai", "van", "le", "ho", "ly", "anh", "nam", "son"}
    )


def test_fide_id_uses_static_current_vie_set():
    assert is_confirmed_vie_fide_id(12415260) is True
    assert is_confirmed_vie_fide_id("12415260") is True
    assert is_confirmed_vie_fide_id(358878) is False
    assert is_confirmed_vie_fide_id("not-an-id") is False
    assert is_confirmed_vie_fide_id(999999999) is False
