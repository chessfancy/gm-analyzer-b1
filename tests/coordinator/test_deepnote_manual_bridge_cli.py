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
