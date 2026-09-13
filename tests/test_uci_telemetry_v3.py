import json

import chess
import chess.engine

from chessgrandmaster.uci_telemetry import (
    info_to_json,
    to_json_safe,
)


def test_uci_serializer_converts_moves_scores_and_wdl_without_repr():
    value = {
        "move": chess.Move.from_uci("e2e4"),
        "score": chess.engine.PovScore(
            chess.engine.Cp(42), chess.WHITE
        ),
        "mate": chess.engine.PovScore(
            chess.engine.Mate(3), chess.BLACK
        ),
        "wdl": chess.engine.PovWdl(
            chess.engine.Wdl(600, 250, 150), chess.WHITE
        ),
    }

    encoded = info_to_json(value)
    decoded = json.loads(encoded)

    assert decoded["move"] == "e2e4"
    assert decoded["score"] == {
        "__type__": "PovScore",
        "relative": {"__type__": "Cp", "cp": 42},
        "turn": "white",
    }
    assert decoded["mate"] == {
        "__type__": "PovScore",
        "relative": {"__type__": "Mate", "moves": 3},
        "turn": "black",
    }
    assert decoded["wdl"] == {
        "__type__": "PovWdl",
        "relative": {
            "__type__": "Wdl",
            "wins": 600,
            "draws": 250,
            "losses": 150,
        },
        "turn": "white",
    }


def test_uci_serializer_recurses_through_nested_containers():
    assert to_json_safe({"items": (1, [True, None, "ok"])}) == {
        "items": [1, [True, None, "ok"]]
    }


def test_uci_serializer_uses_deterministic_unknown_fallback():
    class Unknown:
        def __str__(self):
            return "stable-value"

    assert to_json_safe(Unknown()) == {
        "__type__": (
            "test_uci_telemetry_v3."
            "test_uci_serializer_uses_deterministic_unknown_fallback."
            "<locals>.Unknown"
        ),
        "value": "stable-value",
    }
