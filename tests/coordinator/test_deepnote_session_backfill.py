from __future__ import annotations

import importlib.util
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "oracle" / "deepnote_session_backfill.py"
_spec = importlib.util.spec_from_file_location("deepnote_session_backfill", SCRIPT)
assert _spec is not None and _spec.loader is not None
session = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session)


def test_session_fields_accepts_documented_shape():
    created = {
        "session": {
            "id": "session-1",
            "notebookId": "source-notebook",
            "sessionNotebookId": "copy-notebook",
        },
        "run": {"runId": "initial"},
    }
    assert session._session_fields(created) == (
        "session-1",
        "source-notebook",
        "copy-notebook",
    )


def test_choose_session_block_prefers_code():
    notebook = {
        "blocks": [
            {"id": "markdown", "type": "markdown"},
            {"id": "code", "type": "code"},
        ]
    }
    assert session._choose_session_block(notebook) == "code"


def test_session_fields_rejects_incomplete_shape():
    with pytest.raises(RuntimeError):
        session._session_fields({"session": {"id": "only-id"}})
