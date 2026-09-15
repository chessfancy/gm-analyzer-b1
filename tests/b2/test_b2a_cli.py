from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import sys
import sqlite3
import tomllib
from urllib.parse import urlencode
from urllib.parse import urlparse

import pytest

from chessgrandmaster.b2 import cli_acquire, cli_registry
from chessgrandmaster.b2.registry import Registry
from chessgrandmaster.b2.sources.chess_results import ChessResultsAdapter


FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_URL = (
    "https://s3.chess-results.com/tnr1450909.aspx"
    "?lan=1&art=0&turdet=YES&SNode=S0"
)
NORMALIZED_SOURCE_URL = "https://chess-results.com/tnr1450909.aspx"
SEARCH_URL = "https://chess-results.com/partiesuche.aspx"
DOWNLOAD_URL = "https://chess-results.com/download/fixture-get"
DOWNLOAD_REQUEST_URL = (
    f"{DOWNLOAD_URL}?"
    + urlencode(
        [
            ("format", "pgn"),
            ("session_token", "get-token"),
            ("database_key", "1450909"),
            ("round_from", "1"),
            ("round_to", "7"),
            ("download_action", "Download as PGN-File"),
        ]
    )
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        final_url: str | None = None,
    ) -> None:
        self._body = BytesIO(body)
        self.status = status
        self.headers = headers or {}
        self.final_url = final_url

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.final_url or ""

    def close(self) -> None:
        return None


class FixtureOpener:
    def __init__(self, *, failing_download: bool = False) -> None:
        self.failing_download = failing_download
        self.request_records: list[tuple[str, str]] = []

    def open(self, request, timeout: float):
        url = request.full_url
        method = request.get_method()
        self.request_records.append((method, url))
        parsed = urlparse(url)
        if parsed.path.endswith("/tnr1450909.aspx") and method == "GET":
            return FakeResponse(
                b"<html><h1>Fixture tournament</h1></html>",
                final_url=url,
            )
        if parsed.path.endswith("/partiesuche.aspx") and method == "GET":
            return FakeResponse(
                (FIXTURES / "chess_results_partiesuche.html").read_bytes(),
                final_url=url,
            )
        if parsed.path.endswith("/partiesuche.aspx") and method == "POST":
            return FakeResponse(
                (FIXTURES / "chess_results_database_get.html").read_bytes(),
                final_url=url,
            )
        if parsed.path.endswith("/download/fixture-get") and method == "GET":
            if self.failing_download:
                raise RuntimeError("fixture network failure")
            return FakeResponse(
                (FIXTURES / "chess_results_games.pgn").read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
                final_url=url,
            )
        raise AssertionError(f"unexpected fixture request: {method} {url}")


@pytest.fixture()
def fixture_adapters(monkeypatch):
    def build(*, failing_download: bool = False):
        opener = FixtureOpener(failing_download=failing_download)
        adapter = ChessResultsAdapter(timeout_sec=7.5, opener=opener)
        monkeypatch.setattr(
            cli_acquire,
            "build_adapters",
            lambda: {"chess-results": adapter},
        )
        return opener

    return build


def _all_rows(registry: Registry) -> dict[str, list[tuple]]:
    tables = (
        "sources",
        "tournaments",
        "source_tournaments",
        "source_files",
        "download_attempts",
        "canonical_games",
        "game_occurrences",
        "game_metadata_conflicts",
        "tournament_revisions",
        "tournament_games",
    )
    with registry._connect() as connection:
        return {
            table: [
                tuple(row)
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
            ]
            for table in tables
        }


def _stdout_json(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.out.strip(), "CLI printed nothing on stdout"
    return json.loads(captured.out)


def test_acquire_parser_accepts_literal_reference_and_default_paths():
    parser = cli_acquire.build_parser()

    args = parser.parse_args(["tnr1450909"])

    assert args.source == "tnr1450909"
    assert args.registry == ".cgm/registry.sqlite"
    assert args.root == ".cgm/b2"


def test_acquire_parser_accepts_url_and_explicit_overrides():
    parser = cli_acquire.build_parser()

    args = parser.parse_args(
        [SOURCE_URL, "--registry", "tmp/registry.sqlite", "--root", "tmp/b2"]
    )

    assert args.source == SOURCE_URL
    assert args.registry == "tmp/registry.sqlite"
    assert args.root == "tmp/b2"


def test_acquire_success_prints_one_json_object_and_exits_zero(
    tmp_path, capsys, fixture_adapters
):
    opener = fixture_adapters()
    registry_path = tmp_path / "state" / "registry.sqlite"
    root = tmp_path / "storage" / "b2"

    exit_code = cli_acquire.main(
        [
            "tnr1450909",
            "--registry",
            str(registry_path),
            "--root",
            str(root),
        ]
    )

    payload = _stdout_json(capsys)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["source"]["provider"] == "chess-results"
    assert payload["source"]["external_id"] == "tnr1450909"
    assert payload["source"]["source_url"] == NORMALIZED_SOURCE_URL
    raw_bytes = (FIXTURES / "chess_results_games.pgn").read_bytes()
    raw_sha = payload["raw"]["sha256"]
    assert len(raw_sha) == 64
    assert payload["raw"]["byte_size"] == len(raw_bytes)
    assert raw_sha in payload["raw"]["object_key"]
    assert payload["raw"]["source_file_id"] > 0
    assert payload["raw"]["download_attempt_id"] > 0
    assert payload["canonical"]["revision_id"] > 0
    assert payload["canonical"]["revision_number"] == 1
    assert payload["canonical"]["game_count"] > 0
    assert payload["canonical"]["ply_count"] > 0
    assert len(payload["canonical"]["sha256"]) == 64
    assert payload["tournament"]["status"] == "CANONICALIZED"
    assert payload["tournament"]["id"] > 0
    assert payload["layout"] == {
        "registry": str(registry_path),
        "objects": str(root / "objects"),
        "workspace": str(root / "workspace"),
    }
    assert registry_path.is_file()
    assert (root / "objects").is_dir()
    assert {method for method, _ in opener.request_records} == {"GET", "POST"}

    registry = Registry(registry_path)
    source_file = registry.get_source_file(payload["raw"]["source_file_id"])
    assert source_file["sha256"] == raw_sha
    assert source_file["object_key"] == payload["raw"]["object_key"]
    assert registry.get_tournament_status(payload["tournament"]["id"]) == "CANONICALIZED"


def test_acquire_success_payload_is_serializable_without_raw_rows(
    tmp_path, capsys, fixture_adapters
):
    fixture_adapters()

    cli_acquire.main(
        [
            "tnr1450909",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "b2"),
        ]
    )

    payload = _stdout_json(capsys)

    def assert_primitive(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert isinstance(key, str)
                assert_primitive(item)
            return
        if isinstance(value, list):
            for item in value:
                assert_primitive(item)
            return
        assert value is None or isinstance(value, (str, int, float, bool))

    assert_primitive(payload)
    assert "sqlite3" not in json.dumps(payload)


def test_acquire_failure_prints_structured_json_and_exits_non_zero(
    tmp_path, capsys, fixture_adapters
):
    fixture_adapters(failing_download=True)

    exit_code = cli_acquire.main(
        [
            SOURCE_URL,
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "b2"),
        ]
    )

    payload = _stdout_json(capsys)
    assert exit_code != 0
    assert payload["ok"] is False
    assert payload["error"]["type"] == "RuntimeError"
    assert "fixture network failure" in payload["error"]["message"]
    assert "Traceback" not in json.dumps(payload)
    registry = Registry(tmp_path / "registry.sqlite")
    assert registry.registry_counts()["source_files"] == 0


def test_acquire_unsupported_query_fails_without_creating_rows(
    tmp_path, capsys, fixture_adapters
):
    fixture_adapters()

    exit_code = cli_acquire.main(
        [
            "not-a-source",
            "--registry",
            str(tmp_path / "registry.sqlite"),
            "--root",
            str(tmp_path / "b2"),
        ]
    )

    payload = _stdout_json(capsys)
    assert exit_code != 0
    assert payload["ok"] is False
    assert payload["error"]["type"] == "ValueError"
    registry = Registry(tmp_path / "registry.sqlite")
    assert registry.registry_counts()["sources"] == 0
    assert registry.registry_counts()["tournaments"] == 0


def test_registry_status_all_reports_counts_without_a_tournament_id(
    tmp_path, capsys
):
    registry = Registry(tmp_path / "registry.sqlite")
    source_id = registry.upsert_source("chess-results", "https://chess-results.com")
    tournament_id = registry.upsert_tournament(
        "chess-results-tnr1450909",
        name="Fixture",
        status="DOWNLOADED",
    )
    registry.upsert_source_tournament(
        source_id=source_id,
        tournament_id=tournament_id,
        external_id="tnr1450909",
        source_url="https://chess-results.com/tnr1450909.aspx",
    )

    exit_code = cli_registry.main(["status", "--registry", str(registry.path)])

    payload = _stdout_json(capsys)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["counts"]["sources"] == 1
    assert payload["counts"]["tournaments"] == 1
    assert payload["counts"]["source_tournaments"] == 1
    assert payload["counts"]["canonical_games"] == 0
    assert set(payload["tournament_status_counts"]) == {
        "DISCOVERED",
        "DOWNLOADED",
        "VALIDATED",
        "CANONICALIZED",
        "SHARDED",
        "READY",
    }
    assert payload["tournament_status_counts"]["DOWNLOADED"] == 1
    assert payload["tournament_status_counts"]["READY"] == 0
    assert [row["id"] for row in payload["tournaments"]] == [tournament_id]
    assert payload["tournaments"][0]["slug"] == "chess-results-tnr1450909"
    assert payload["tournaments"][0]["status"] == "DOWNLOADED"


def test_registry_status_one_reports_provenance_and_revisions(
    tmp_path, capsys, fixture_adapters
):
    fixture_adapters()
    registry_path = tmp_path / "registry.sqlite"
    root = tmp_path / "b2"
    assert (
        cli_acquire.main(
            ["tnr1450909", "--registry", str(registry_path), "--root", str(root)]
        )
        == 0
    )
    acquisition = _stdout_json(capsys)
    tournament_id = acquisition["tournament"]["id"]

    exit_code = cli_registry.main(
        ["status", str(tournament_id), "--registry", str(registry_path)]
    )

    payload = _stdout_json(capsys)
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["tournament"]["id"] == tournament_id
    assert payload["tournament"]["slug"] == "chess-results-tnr1450909"
    assert payload["tournament"]["status"] == "CANONICALIZED"
    assert payload["tournament"]["priority_score"] == 0
    assert payload["tournament"]["priority_reasons"] == []
    assert payload["source_tournaments"][0]["external_id"] == "tnr1450909"
    assert payload["source_tournaments"][0]["source_name"] == "chess-results"
    assert (
        payload["source_tournaments"][0]["source_url"] == NORMALIZED_SOURCE_URL
    )
    assert payload["source_files"][0]["id"] == acquisition["raw"]["source_file_id"]
    assert payload["source_files"][0]["sha256"] == acquisition["raw"]["sha256"]
    assert payload["source_files"][0]["byte_size"] == acquisition["raw"]["byte_size"]
    assert (
        payload["source_files"][0]["object_key"] == acquisition["raw"]["object_key"]
    )
    assert len(payload["revisions"]) == 1
    revision = payload["revisions"][0]
    assert revision["revision_id"] == acquisition["canonical"]["revision_id"]
    assert revision["revision_number"] == acquisition["canonical"]["revision_number"]
    assert revision["canonical_sha256"] == acquisition["canonical"]["sha256"]
    assert revision["canonicalization_policy"] == "canonical_pgn_v1"
    assert revision["game_count"] == acquisition["canonical"]["game_count"]


def test_registry_status_unknown_tournament_returns_structured_failure(
    tmp_path, capsys
):
    registry_path = tmp_path / "registry.sqlite"
    Registry(registry_path)

    exit_code = cli_registry.main(
        ["status", "4242", "--registry", str(registry_path)]
    )

    payload = _stdout_json(capsys)
    assert exit_code != 0
    assert payload["ok"] is False
    assert payload["error"]["type"] == "not_found"
    assert "4242" in payload["error"]["message"]


def test_registry_status_is_read_only(tmp_path, capsys, fixture_adapters):
    fixture_adapters()
    registry_path = tmp_path / "registry.sqlite"
    assert (
        cli_acquire.main(
            [
                "tnr1450909",
                "--registry",
                str(registry_path),
                "--root",
                str(tmp_path / "b2"),
            ]
        )
        == 0
    )
    capsys.readouterr()
    registry = Registry(registry_path)
    before = _all_rows(registry)
    before_bytes = registry_path.read_bytes()
    with registry._connect() as connection:
        before_meta = connection.execute(
            "SELECT key, value FROM registry_meta ORDER BY key"
        ).fetchall()
        before_user_version = connection.execute(
            "PRAGMA user_version"
        ).fetchone()[0]

    assert cli_registry.main(["status", "--registry", str(registry_path)]) == 0
    assert (
        cli_registry.main(
            ["status", "1", "--registry", str(registry_path)]
        )
        == 0
    )
    capsys.readouterr()

    assert _all_rows(registry) == before
    assert registry_path.read_bytes() == before_bytes
    with registry._connect() as connection:
        assert connection.execute(
            "SELECT key, value FROM registry_meta ORDER BY key"
        ).fetchall() == before_meta
        assert connection.execute("PRAGMA user_version").fetchone()[0] == before_user_version


def test_registry_open_read_only_requires_existing_file(tmp_path):
    registry_path = tmp_path / "missing.sqlite"

    with pytest.raises(FileNotFoundError):
        Registry.open_read_only(registry_path)

    assert not registry_path.exists()


def test_registry_open_read_only_rejects_writes(tmp_path):
    registry_path = tmp_path / "registry.sqlite"
    Registry(registry_path)
    before_bytes = registry_path.read_bytes()

    read_only = Registry.open_read_only(registry_path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        read_only.upsert_source("must-not-write")

    assert registry_path.read_bytes() == before_bytes


def test_registry_status_does_not_initialize_existing_partial_file(tmp_path, capsys):
    registry_path = tmp_path / "partial.sqlite"
    with sqlite3.connect(registry_path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('untouched')")
    before_bytes = registry_path.read_bytes()

    exit_code = cli_registry.main(["status", "--registry", str(registry_path)])

    payload = _stdout_json(capsys)
    assert exit_code != 0
    assert payload["ok"] is False
    assert registry_path.read_bytes() == before_bytes
    with sqlite3.connect(registry_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("sentinel",)]


def test_registry_status_rejects_unsupported_schema_version(tmp_path, capsys):
    registry_path = tmp_path / "unsupported.sqlite"
    Registry(registry_path)
    with sqlite3.connect(registry_path) as connection:
        connection.execute("PRAGMA user_version = 2")
    before_bytes = registry_path.read_bytes()

    exit_code = cli_registry.main(["status", "--registry", str(registry_path)])

    payload = _stdout_json(capsys)
    assert exit_code != 0
    assert payload["ok"] is False
    assert payload["error"]["type"] == "RuntimeError"
    assert "unsupported registry schema version: 2" in payload["error"]["message"]
    assert "Traceback" not in json.dumps(payload)
    assert registry_path.read_bytes() == before_bytes


def test_cli_commands_do_not_invoke_b1_analysis(
    tmp_path, capsys, fixture_adapters
):
    for module_name in (
        "chessgrandmaster.production_pipeline",
        "chessgrandmaster.analyzer",
        "chessgrandmaster.engine_worker",
        "chessgrandmaster.benchmark",
    ):
        sys.modules.pop(module_name, None)

    fixture_adapters()
    registry_path = tmp_path / "registry.sqlite"

    assert (
        cli_acquire.main(
            [
                "tnr1450909",
                "--registry",
                str(registry_path),
                "--root",
                str(tmp_path / "b2"),
            ]
        )
        == 0
    )
    assert cli_registry.main(["status", "--registry", str(registry_path)]) == 0
    capsys.readouterr()

    for module_name in (
        "chessgrandmaster.production_pipeline",
        "chessgrandmaster.analyzer",
        "chessgrandmaster.engine_worker",
        "chessgrandmaster.benchmark",
    ):
        assert module_name not in sys.modules


def test_b2_cli_modules_do_not_use_private_registry_sql():
    for module in (cli_acquire, cli_registry):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "._connect(" not in source
        assert "sqlite3" not in source


def test_project_scripts_keep_existing_entry_points_and_add_b2_cli():
    scripts = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["scripts"]

    assert scripts["cgm-analyze"] == "chessgrandmaster.cli:main"
    assert scripts["cgm-engine"] == "chessgrandmaster.engine_manifest:main"
    assert scripts["cgm-bench"] == "chessgrandmaster.benchmark_cli:main"
    assert scripts["cgm-acquire"] == "chessgrandmaster.b2.cli_acquire:main"
    assert scripts["cgm-registry"] == "chessgrandmaster.b2.cli_registry:main"


def test_cli_help_exits_zero_without_touching_the_registry(capsys):
    with pytest.raises(SystemExit) as acquire_exit:
        cli_acquire.main(["--help"])
    with pytest.raises(SystemExit) as registry_exit:
        cli_registry.main(["--help"])

    captured = capsys.readouterr()
    assert acquire_exit.value.code == 0
    assert registry_exit.value.code == 0
    assert ".cgm/registry.sqlite" in captured.out
    assert ".cgm/b2" in captured.out
