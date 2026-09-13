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
    assert called["kwargs"]["workers"] == 2
    assert called["kwargs"]["hash_mb"] == 256
    assert called["kwargs"]["depth"] == 18


def test_cli_passes_workers_override_to_pipeline(monkeypatch, tmp_path):
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

    rc = cli.main(
        [
            "--workers", "4",
            "--hash-mb", "512",
            "--depth", "19",
            str(pgn),
        ]
    )

    assert rc == 0
    assert Path(called["pgn_input"]) == pgn
    assert called["kwargs"]["workers"] == 4
    assert called["kwargs"]["hash_mb"] == 512
    assert called["kwargs"]["depth"] == 19


def test_cli_rejects_nonpositive_production_knobs(tmp_path):
    from chessgrandmaster import cli

    pgn = tmp_path / "tournament.pgn"
    pgn.write_text('[Event "Test"]\n\n*\n', encoding="utf-8")

    import pytest

    for flag in ("--workers", "--hash-mb", "--depth"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([flag, "0", str(pgn)])

        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([flag, "-1", str(pgn)])


def test_cli_resolves_named_profile_and_explicit_overrides(monkeypatch, tmp_path):
    from chessgrandmaster import cli

    pgn = tmp_path / "tournament.pgn"
    pgn.write_text('[Event "Test"]\n\n*\n', encoding="utf-8")
    called = {}

    def fake_run_pipeline(pgn_input, **kwargs):
        called["kwargs"] = kwargs
        return {"ok": True}

    monkeypatch.setattr(cli, "run_pipeline", fake_run_pipeline)
    assert cli.main([
        "--platform-profile", "molab",
        "--hash-mb", "1024",
        "--threads", "1",
        str(pgn),
    ]) == 0

    assert called["kwargs"]["workers"] == 4
    assert called["kwargs"]["threads"] == 1
    assert called["kwargs"]["hash_mb"] == 1024
