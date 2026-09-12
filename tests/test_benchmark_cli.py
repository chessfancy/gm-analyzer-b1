

def test_top_level_help_mentions_compare(capsys):
    from chessgrandmaster import benchmark_cli

    assert benchmark_cli.main(["--help"]) == 0

    output = capsys.readouterr().out

    assert "qualify" in output
    assert "compare" in output


def test_compare_dispatch_does_not_require_benchmark_engine_module(
    monkeypatch,
):
    from chessgrandmaster import benchmark_cli

    called = {}

    def fake_compare(argv):
        called["argv"] = argv
        return 7

    monkeypatch.setattr(
        benchmark_cli,
        "run_compare",
        fake_compare,
    )

    assert (
        benchmark_cli.main([
            "compare",
            "a.json",
            "b.json",
        ])
        == 7
    )

    assert called["argv"] == [
        "a.json",
        "b.json",
    ]
