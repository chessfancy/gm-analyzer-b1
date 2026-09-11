from pathlib import Path


def test_cli_runs_pipeline_for_input_pgn(monkeypatch, tmp_path):
    from chessgrandmaster import cli

    pgn = tmp_path / "tournament.pgn"
    pgn.write_text('[Event "Test"]\n\n*\n', encoding="utf-8")

    called = {}

    def fake_run_pipeline(pgn_input, **kwargs):
        called["pgn_input"] = pgn_input
        called["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(
        cli,
        "run_pipeline",
        fake_run_pipeline,
    )

    rc = cli.main([str(pgn)])

    assert rc == 0
    assert Path(called["pgn_input"]) == pgn
