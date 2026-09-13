import gzip
import json

import chess
import chess.engine

from chessgrandmaster.uci_telemetry import (
    UciEventArchive,
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


def test_uci_event_archive_is_valid_gzip_jsonl_with_search_context(tmp_path):
    path = tmp_path / "uci" / "worker_0_session.jsonl.gz"
    archive = UciEventArchive(path, archive_root=tmp_path)
    context = {
        "run_id": 7,
        "execution_id": "exec-1",
        "worker_id": 0,
        "engine_session_id": "session-1",
        "game_id": 11,
        "source_game_index": 2,
        "move_id": 31,
        "ply": 4,
        "source_search": "primary",
    }

    archive.write("search_start", context)
    archive.write_info(context, {
        "multipv": 1,
        "depth": 12,
        "score": chess.engine.PovScore(
            chess.engine.Cp(20), chess.WHITE
        ),
        "pv": [chess.Move.from_uci("e2e4")],
    })
    archive.write_info(
        {**context, "source_search": "post_move"},
        {"multipv": 1, "depth": 12, "pv": []},
    )
    archive.write("search_end", context)
    manifest = archive.close()

    with gzip.open(path, "rt", encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream]

    assert [record["event"] for record in records] == [
        "search_start", "info", "info", "search_end"
    ]
    assert manifest["event_count"] == 2
    assert manifest["relative_path"] == "uci/worker_0_session.jsonl.gz"
    assert records[1]["info"]["score"]["relative"]["cp"] == 20
    assert records[1]["move_id"] == 31
    assert records[1]["source_search"] == "primary"
    assert records[2]["source_search"] == "post_move"
    assert records[1]["execution_id"] == "exec-1"
