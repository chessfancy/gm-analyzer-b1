from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "oracle" / "deepnote_dispatch.py"

_spec = importlib.util.spec_from_file_location("cgm_deepnote_dispatch", SCRIPT)
assert _spec is not None and _spec.loader is not None
deepnote_dispatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deepnote_dispatch)


def test_remote_code_reuses_deepnote_python_environment():
    code = deepnote_dispatch._remote_code(
        "cgm-worker/jobs/attempt-1",
        "cgm-worker/results/attempt-1.zip",
        "a" * 40,
        "attempt-1-deadbeef",
    )

    assert "VENV = Path(sys.prefix)" in code
    assert 'env["CGM_VENV"] = str(VENV)' in code
    assert 'env["CGM_BOOTSTRAP_PYTHON"] = sys.executable' in code
    assert 'env["CGM_INSTALL_DEV"] = "0"' in code
    assert 'env["CGM_SKIP_PACKAGE_INSTALL"] = "1"' in code
    assert 'run(["bash", "scripts/setup_platform.sh"], cwd=REPO, env=env)' in code
    assert '"-m", "venv"' not in code
    assert "--without-pip" not in code
    assert '"--python"' not in code


def test_download_file_uses_resumable_curl(monkeypatch, tmp_path):
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output = Path(command[command.index("-o") + 1])
        output.write_bytes(b"result")
        return Result()

    monkeypatch.setattr(deepnote_dispatch.subprocess, "run", fake_run)
    monkeypatch.setattr(deepnote_dispatch, "_token", lambda: "secret-token")

    target = tmp_path / "result.zip"
    deepnote_dispatch._download_file("project", "results/result.zip", target)

    assert target.read_bytes() == b"result"
    command, kwargs = calls[0]
    assert "--continue-at" in command
    assert "--retry-all-errors" in command
    assert "Authorization: Bearer secret-token" in command
    assert kwargs["check"] is False


def test_run_error_details_includes_top_level_deepnote_error(monkeypatch):
    error = "Detached run terminated: DETACHED_TIMEOUT_PREEMPTIBLE"

    def fake_request_json(method, path, payload=None):
        assert method == "GET"
        assert "snapshotDelivery=blocks" in path
        assert payload is None
        return {
            "run": {
                "error": error,
                "snapshotBlocks": [],
            }
        }

    monkeypatch.setattr(deepnote_dispatch, "_request_json", fake_request_json)

    assert deepnote_dispatch._run_error_details("run-123") == error