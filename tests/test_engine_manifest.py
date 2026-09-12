import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run_python(code):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")

    return subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        env=env,
    )


def test_engine_manifest_has_stockfish_19_contract():
    result = run_python(
        """
import json
from chessgrandmaster.engine_manifest import configured_engine
print(json.dumps(configured_engine()))
"""
    )

    assert result.returncode == 0, result.stderr

    data = json.loads(result.stdout)

    assert data["name"] == "Stockfish"
    assert data["version"] == "19"
    assert data["label"] == "Stockfish 19"
    assert data["uci_name"] == "Stockfish 19"
    assert data["output_tag"] == "SF19"


def test_engine_verification_accepts_configured_uci_name(tmp_path):
    engine = tmp_path / "stockfish"

    engine.write_text(
        """#!/usr/bin/env bash
cat >/dev/null
printf 'id name Stockfish 19\\n'
printf 'id author Test\\n'
printf 'uciok\\n'
""",
        encoding="utf-8",
    )

    engine.chmod(0o755)

    code = f"""
from chessgrandmaster.engine_manifest import verify_engine_binary
info = verify_engine_binary({str(engine)!r})
assert info["uci_name"] == "Stockfish 19"
print("OK")
"""

    result = run_python(code)

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_engine_verification_rejects_wrong_version(tmp_path):
    engine = tmp_path / "stockfish"

    engine.write_text(
        """#!/usr/bin/env bash
cat >/dev/null
printf 'id name Stockfish 18\\n'
printf 'id author Test\\n'
printf 'uciok\\n'
""",
        encoding="utf-8",
    )

    engine.chmod(0o755)

    code = f"""
from chessgrandmaster.engine_manifest import verify_engine_binary

try:
    verify_engine_binary({str(engine)!r})
except RuntimeError:
    print("REJECTED")
else:
    raise SystemExit("wrong engine version was accepted")
"""

    result = run_python(code)

    assert result.returncode == 0, result.stderr
    assert "REJECTED" in result.stdout


def test_release_checksum_is_explicitly_archive_checksum():
    from chessgrandmaster.engine_manifest import platform_spec

    spec = platform_spec(
        system="linux",
        machine="x86_64",
    )

    assert spec["archive_sha256"] == (
        "9defc0d4e55d49c65a6d042f3e571a39"
        "fcea499ade6dbe741b53b8c65e03611f"
    )

    assert "sha256" not in spec


def test_default_engine_install_path_uses_cgm_home(monkeypatch, tmp_path):
    from chessgrandmaster.engine_manifest import default_engine_install_path

    workspace = tmp_path / "workspace"
    monkeypatch.delenv("CGM_ENGINE_INSTALL_PATH", raising=False)
    monkeypatch.setenv("CGM_HOME", str(workspace))

    assert default_engine_install_path() == (
        workspace / "bin" / "stockfish"
    ).resolve()


def test_default_engine_install_path_prefers_explicit_path(monkeypatch, tmp_path):
    from chessgrandmaster.engine_manifest import default_engine_install_path

    explicit = tmp_path / "engines" / "stockfish"
    monkeypatch.setenv("CGM_HOME", str(tmp_path / "workspace"))
    monkeypatch.setenv("CGM_ENGINE_INSTALL_PATH", str(explicit))

    assert default_engine_install_path() == explicit.resolve()


def test_resolve_installed_engine_finds_managed_path(monkeypatch, tmp_path):
    from chessgrandmaster.engine_manifest import resolve_installed_engine

    engine = tmp_path / "managed" / "stockfish"
    engine.parent.mkdir(parents=True)
    engine.write_text("", encoding="utf-8")
    engine.chmod(0o755)

    monkeypatch.delenv("CGM_STOCKFISH", raising=False)
    monkeypatch.setenv("CGM_ENGINE_INSTALL_PATH", str(engine))
    monkeypatch.setenv("PATH", "")

    assert resolve_installed_engine() == engine.resolve()
