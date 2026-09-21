import json
from pathlib import Path

from chessgrandmaster.b2.canonicalize import canonicalize_source_file
from chessgrandmaster.b2.cli_package import main
from chessgrandmaster.b2.registry import Registry


PGN = """[Event \"CLI Fixture\"]
[Site \"Test\"]
[Date \"2026.09.21\"]
[Round \"1\"]
[White \"White\"]
[Black \"Black\"]
[Result \"*\"]

1. e4 e5 *
"""


def seed_cli_registry(tmp_path: Path):
    registry = Registry(tmp_path / "registry.sqlite")
    tournament_id = registry.upsert_tournament(
        "cli-fixture",
        name="CLI Fixture",
        status="CANONICALIZED",
    )
    source_id = registry.upsert_source("fixture", "https://example.invalid")
    source_tournament_id = registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id="cli-fixture",
    )
    raw_path = tmp_path / "input.pgn"
    raw_path.write_text(PGN, encoding="utf-8", newline="")
    raw_bytes = raw_path.read_bytes()
    source_file_id = registry.record_source_file(
        source_tournament_id=source_tournament_id,
        object_key="raw/cli/input.pgn",
        filename="input.pgn",
        sha256=__import__("hashlib").sha256(raw_bytes).hexdigest(),
        byte_size=len(raw_bytes),
    )
    canonicalize_source_file(
        registry,
        tournament_id,
        source_file_id,
        raw_path,
        tmp_path / "canonical.pgn",
    )
    return registry


def test_cli_local_packaging_prints_canonical_json_summary(tmp_path, capsys):
    seed_cli_registry(tmp_path)
    code = main(
        [
            "cli-fixture",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "objects"),
            "--store",
            "local",
            "--target-plies",
            "3000",
        ]
    )

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["tournament_id"] == "cli-fixture"
    assert output["revision"] == 1
    assert output["state"] == "READY"
    assert output["shards"] == 1
    assert output["games"] == 1
    assert output["plies"] == 2


def test_cli_parser_supports_optional_s3_form():
    from chessgrandmaster.b2.cli_package import build_parser

    args = build_parser().parse_args(
        [
            "remote",
            "--registry",
            "registry.sqlite",
            "--root",
            "objects",
            "--store",
            "s3",
            "--s3-bucket",
            "bucket",
            "--s3-prefix",
            "prefix",
            "--s3-endpoint-url",
            "https://example.invalid",
        ]
    )

    assert args.store == "s3"
    assert args.s3_bucket == "bucket"
    assert args.s3_prefix == "prefix"
    assert args.s3_endpoint_url == "https://example.invalid"
