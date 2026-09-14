import io

import chess.pgn

from chessgrandmaster.b2.identity import (
    FINGERPRINT_VERSION,
    identify_game,
)


def read_one(text):
    game = chess.pgn.read_game(io.StringIO(text))
    assert game is not None
    assert not game.errors
    return game


def test_fingerprint_ignores_headers_comments_nags_and_variations():
    first = read_one(
        '''
[Event "Source A"]
[White "Nguyen A"]
[Black "Player B"]
[Result "1-0"]

1. e4 $1 {comment} e5 (1... c5) 2. Nf3 Nc6 1-0
'''
    )
    second = read_one(
        '''
[Event "Different spelling"]
[White "NGUYEN, A"]
[Black "Player B"]
[Result "*"]

1.e4 e5 2.Nf3 Nc6 *
'''
    )

    assert identify_game(first).fingerprint == identify_game(second).fingerprint


def test_fingerprint_changes_when_mainline_changes():
    first = read_one('[Event "Same"]\n[Round "1"]\n\n1. e4 e5 2. Nf3 Nc6 *')
    second = read_one('[Event "Same"]\n[Round "1"]\n\n1. e4 c5 2. Nf3 d6 *')

    assert identify_game(first).fingerprint != identify_game(second).fingerprint


def test_truncated_prefix_is_not_merged_with_complete_game():
    prefix = read_one('1. e4 e5 2. Nf3 *')
    complete = read_one('1. e4 e5 2. Nf3 Nc6 *')

    assert identify_game(prefix).fingerprint != identify_game(complete).fingerprint


def test_normalized_default_start_does_not_depend_on_fen_header_presence():
    default_start = read_one('[Result "*"]\n\n1. e4 *')
    explicit_start = read_one(
        '''
[SetUp "1"]
[FEN "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"]
[Result "*"]

1. e4 *
'''
    )

    assert identify_game(default_start).fingerprint == identify_game(explicit_start).fingerprint


def test_identity_exposes_version_content_and_ply_count():
    game = read_one('1. e4 e5 2. Nf3 *')

    identity = identify_game(game)

    assert identity.fingerprint_version == FINGERPRINT_VERSION == "game_fingerprint_v1"
    assert identity.variant == "standard"
    assert identity.mainline_uci == ("e2e4", "e7e5", "g1f3")
    assert identity.ply_count == 3
    assert len(identity.fingerprint) == 64
