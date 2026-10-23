from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/oracle/deepnote_manual_bridge.py"
WRAPPER = ROOT / "scripts/oracle/run_deepnote_manual_bridge.sh"


def test_deepnote_manual_bridge_cli_exposes_safe_defaults():
    proc = subprocess.run([sys.executable, str(CLI), "--help"], cwd=ROOT, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    source = CLI.read_text(encoding="utf-8")
    assert "/home/ubuntu/data/cgm/coordinator.sqlite" in source
    assert "/home/ubuntu/.config/cgm/deepnote-worker.json" in source
    assert "deepnote_api_key" in source
    assert "DeepnoteManualBridge" in source
    assert "print(_token" not in source


def test_deepnote_manual_bridge_wrapper_is_lock_protected():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "flock" in text
    assert "deepnote_manual_bridge.py" in text
    assert "deepnote-manual-bridge.log" in text
    assert "--auto-trigger" in text


def test_deepnote_storage_retries_auto_renamed_upload(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("test_deepnote_bridge_cli", CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = tmp_path / "current.json"
    source.write_text("{}", encoding="utf-8")
    requested = "cgm-manual/inbox/current.json"
    renamed = "cgm-manual/inbox/current-20260928.json"
    uploads = iter([renamed, requested])
    deleted = []

    monkeypatch.setattr(module.dn, "_delete_file", lambda project, path: deleted.append(path))
    monkeypatch.setattr(module.dn, "_upload_file", lambda project, path, src: next(uploads))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    storage = module.DeepnoteProjectStorage("project")
    storage.upload(requested, source)

    assert renamed in deleted
    assert deleted.count(requested) >= 2


def test_deepnote_storage_retries_http_429_with_retry_after(tmp_path, monkeypatch):
    import importlib.util
    import urllib.error

    spec = importlib.util.spec_from_file_location("test_deepnote_bridge_rate_limit", CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    source = tmp_path / "archive.zip"
    source.write_bytes(b"test archive")
    requested = "cgm-manual/inbox/archive.zip"
    calls = {"upload": 0}
    sleeps = []

    monkeypatch.setattr(module.dn, "_delete_file", lambda project, path: None)

    def flaky_upload(project, path, src):
        calls["upload"] += 1
        if calls["upload"] == 1:
            raise urllib.error.HTTPError(
                "https://api.deepnote.com/v2/files", 429, "Too Many Requests",
                {"Retry-After": "2"}, None,
            )
        return path

    monkeypatch.setattr(module.dn, "_upload_file", flaky_upload)
    monkeypatch.setattr(module.time, "sleep", sleeps.append)

    storage = module.DeepnoteProjectStorage("project")
    storage.upload(requested, source)

    assert calls["upload"] == 2
    assert sleeps == [2.0]


def _load_deepnote_bridge_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("test_deepnote_bridge_autotrigger", CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_auto_trigger_recovers_same_batch_after_terminal_error_once(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    module = _load_deepnote_bridge_module()
    batch_id = "deepnote-manual-batch-recovery-test"
    state_path = tmp_path / "autotrigger.json"
    state_path.write_text(json.dumps({"batch_id": batch_id, "run_id": "old-run", "status": "pending"}))
    progress = json.dumps({"batch_id": batch_id, "state": "interrupted"}).encode()
    calls = []

    class Storage:
        def read_bytes(self, path):
            assert path.endswith("/progress.json")
            return progress

    def request(method, path, payload=None):
        calls.append((method, path))
        if method == "GET" and path.endswith("/runs?pageSize=20"):
            return {"runs": [{"runId": "old-run", "status": "error"}]}
        if method == "GET" and path == "/notebooks/notebook":
            return {"notebook": {"blocks": [{"id": "analyze", "content": "run_deepnote_manual_cycle.py"}]}}
        if method == "POST" and path == "/runs":
            return {"runId": "recovery-run", "status": "pending"}
        raise AssertionError((method, path))

    monkeypatch.setattr(module.dn, "_request_json", request)
    result = module.maybe_auto_trigger(
        current=SimpleNamespace(batch_id=batch_id), storage=Storage(),
        config={"notebook_id": "notebook"}, state_path=state_path,
    )
    assert result["triggered"] is True
    assert result["run_id"] == "recovery-run"
    assert result["retry_count"] == 1
    assert json.loads(state_path.read_text())["retry_count"] == 1
    assert calls.count(("POST", "/runs")) == 1


def test_auto_trigger_does_not_retry_terminal_failure_repeatedly(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    module = _load_deepnote_bridge_module()
    batch_id = "deepnote-manual-batch-recovery-test"
    state_path = tmp_path / "autotrigger.json"
    state_path.write_text(json.dumps({
        "batch_id": batch_id, "run_id": "recovery-run", "status": "error", "retry_count": 1,
    }))

    class Storage:
        def read_bytes(self, path):
            return json.dumps({"batch_id": batch_id, "state": "interrupted"}).encode()

    monkeypatch.setattr(module.dn, "_request_json", lambda method, path, payload=None: {
        "runs": [{"runId": "recovery-run", "status": "error"}]
    })
    result = module.maybe_auto_trigger(
        current=SimpleNamespace(batch_id=batch_id), storage=Storage(),
        config={"notebook_id": "notebook"}, state_path=state_path,
    )
    assert result["triggered"] is False
    assert result["reason"] == "automatic-retry-limit-reached-review-required"
