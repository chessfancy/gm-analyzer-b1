from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "oracle" / "codespaces_dispatch.py"

_spec = importlib.util.spec_from_file_location("cgm_codespaces_dispatch", SCRIPT)
assert _spec is not None and _spec.loader is not None
codespaces_dispatch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(codespaces_dispatch)


def test_remote_command_pins_sha_and_uses_codespaces_provider():
    sha = "a" * 40
    code = codespaces_dispatch._remote_command("0123456789ab", sha)

    assert f"git reset --hard {sha}" in code
    assert "--provider codespaces" in code
    assert "scripts/workers/run_job_bundle.py" in code
    assert "engine_manifest verify" in code


def test_remote_command_rejects_untrusted_identifiers():
    try:
        codespaces_dispatch._remote_command("bad;token", "a" * 40)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid token should fail")

    try:
        codespaces_dispatch._remote_command("0123456789ab", "not-a-sha")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid SHA should fail")


def test_copy_to_remote_uses_expand_for_tilde(monkeypatch, tmp_path):
    export_dir = tmp_path / "bundle"
    export_dir.mkdir()
    (export_dir / "job.json").write_text("{}\n", encoding="utf-8")
    (export_dir / "input.pgn").write_text("*\n", encoding="utf-8")

    ssh_calls = []
    run_calls = []

    monkeypatch.setattr(
        codespaces_dispatch,
        "_ssh",
        lambda *args, **kwargs: ssh_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        codespaces_dispatch,
        "_run",
        lambda args, **kwargs: run_calls.append((list(args), kwargs)),
    )

    codespaces_dispatch._copy_to_remote(
        "worker-space",
        export_dir,
        "~/cgm-worker/jobs/deadbeef",
    )

    assert ssh_calls
    args, kwargs = run_calls[0]
    assert args[:5] == [
        codespaces_dispatch.GH,
        "codespace",
        "cp",
        "-e",
        "-c",
    ]
    assert "worker-space" in args
    assert args[-1] == "remote:~/cgm-worker/jobs/deadbeef/"


def test_ensure_available_starts_shutdown_codespace(monkeypatch):
    states = iter(["Shutdown", "Starting", "Available"])
    run_calls = []

    monkeypatch.setattr(
        codespaces_dispatch,
        "_codespace_state",
        lambda name: next(states),
    )
    monkeypatch.setattr(
        codespaces_dispatch,
        "_run",
        lambda args, **kwargs: run_calls.append((list(args), kwargs)),
    )
    monkeypatch.setattr(codespaces_dispatch.time, "sleep", lambda _: None)

    codespaces_dispatch._ensure_available(
        "worker-space",
        poll_seconds=0,
        timeout_seconds=60,
    )

    assert run_calls
    assert run_calls[0][0][-2:] == [
        "POST",
        "/user/codespaces/worker-space/start",
    ]
