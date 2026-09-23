from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "oracle" / "refresh_lichess_olympiad.py"

_spec = importlib.util.spec_from_file_location("cgm_olympiad_refresh", SCRIPT)
assert _spec is not None and _spec.loader is not None
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)


def test_extract_rounds_from_embedded_lichess_payload():
    html = (
        '<script>{"tour":{"rounds":['
        '{"id":"a","name":"Round 1","finished":true,'
        '"url":"https://lichess.org/broadcast/x/round-1/a"},'
        '{"id":"b","name":"Round 2","finished":false,'
        '"url":"https://lichess.org/broadcast/x/round-2/b"}'
        ']}}</script>'
    )

    rounds = refresh._extract_rounds_from_html(html)

    assert [item["id"] for item in rounds] == ["a", "b"]


def test_round_number_is_parsed_from_round_slug():
    assert (
        refresh._round_number(
            "https://lichess.org/broadcast/event/round-11/abc123"
        )
        == 11
    )
