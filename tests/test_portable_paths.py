from pathlib import Path

import pytest

from chessgrandmaster import production_pipeline as pp


def test_resolve_input_path_accepts_existing_file(tmp_path):
    pgn = tmp_path / "games.pgn"
    pgn.write_text('[Event "Test"]\n\n*\n', encoding="utf-8")

    assert pp.resolve_input_path(pgn) == pgn.resolve()


def test_default_workspace_root_uses_cgm_home(monkeypatch, tmp_path):
    home = tmp_path / "workspace"
    monkeypatch.setenv("CGM_HOME", str(home))

    assert pp.default_workspace_root() == home.resolve()


def test_resolve_engine_binary_uses_environment(monkeypatch, tmp_path):
    engine = tmp_path / "stockfish"
    engine.write_text("", encoding="utf-8")
    engine.chmod(0o755)

    monkeypatch.setenv("CGM_STOCKFISH", str(engine))

    assert pp.resolve_engine_binary() == engine.resolve()


def test_resolve_engine_binary_fails_cleanly_when_missing(monkeypatch):
    monkeypatch.delenv("CGM_STOCKFISH", raising=False)
    monkeypatch.setenv("PATH", "")

    with pytest.raises(FileNotFoundError, match="Stockfish"):
        pp.resolve_engine_binary()
