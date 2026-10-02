from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/oracle/deepnote_manual_bridge.py"


def load_module():
    spec = importlib.util.spec_from_file_location("deepnote_manual_bridge_cli", CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EmptyStorage:
    def read_bytes(self, path: str) -> bytes | None:
        return None


def test_auto_trigger_starts_only_analyze_block_in_live_mode(tmp_path, monkeypatch):
    module = load_module()
    calls = []

    def fake_request(method, path, payload=None, **kwargs):
        calls.append((method, path, payload))
        if method == "GET" and path.endswith("/runs?pageSize=20"):
            return {"runs": [{"runId": "old", "status": "success"}]}
        if method == "GET" and path == "/notebooks/notebook-1":
            return {"notebook": {"blocks": [
                {"id": "load", "type": "code", "content": "print('load')"},
                {"id": "analyze", "type": "code", "content": "run_deepnote_manual_cycle.py --work-root /work"},
            ]}}
        if method == "POST" and path == "/runs":
            return {"runId": "run-new", "status": "pending", "createdAt": "2026-10-02T08:10:00Z"}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(module.dn, "_request_json", fake_request)
    state_path = tmp_path / "autotrigger.json"
    result = module.maybe_auto_trigger(
        current=SimpleNamespace(batch_id="batch-1"),
        storage=EmptyStorage(),
        config={"notebook_id": "notebook-1"},
        state_path=state_path,
    )

    assert result["triggered"] is True
    assert result["batch_id"] == "batch-1"
    assert result["run_id"] == "run-new"
    assert ("POST", "/runs", {
        "notebookId": "notebook-1",
        "detached": False,
        "blockIds": ["analyze"],
    }) in calls
    saved = json.loads(state_path.read_text())
    assert saved["batch_id"] == "batch-1"
    assert saved["run_id"] == "run-new"


def test_auto_trigger_does_not_repeat_same_batch(tmp_path, monkeypatch):
    module = load_module()
    state_path = tmp_path / "autotrigger.json"
    state_path.write_text(json.dumps({
        "batch_id": "batch-1",
        "run_id": "run-existing",
        "status": "running",
    }), encoding="utf-8")

    def should_not_call(*args, **kwargs):
        raise AssertionError("Deepnote API must not be called for an already-triggered batch")

    monkeypatch.setattr(module.dn, "_request_json", should_not_call)
    result = module.maybe_auto_trigger(
        current=SimpleNamespace(batch_id="batch-1"),
        storage=EmptyStorage(),
        config={"notebook_id": "notebook-1"},
        state_path=state_path,
    )

    assert result == {
        "triggered": False,
        "batch_id": "batch-1",
        "reason": "already-triggered",
        "run_id": "run-existing",
    }


class CompleteStorage:
    def read_bytes(self, path: str) -> bytes | None:
        return json.dumps({
            "schema_version": "cgm-deepnote-progress-1",
            "batch_id": "batch-1",
            "state": "complete",
        }).encode("utf-8")


def test_auto_trigger_skips_completed_current_batch(tmp_path, monkeypatch):
    module = load_module()

    def should_not_call(*args, **kwargs):
        raise AssertionError("Deepnote API must not run for a completed current batch")

    monkeypatch.setattr(module.dn, "_request_json", should_not_call)
    result = module.maybe_auto_trigger(
        current=SimpleNamespace(batch_id="batch-1"),
        storage=CompleteStorage(),
        config={"notebook_id": "notebook-1"},
        state_path=tmp_path / "autotrigger.json",
    )

    assert result == {
        "triggered": False,
        "batch_id": "batch-1",
        "reason": "already-complete",
    }


def test_auto_trigger_skips_when_notebook_run_is_active(tmp_path, monkeypatch):
    module = load_module()
    calls = []

    def fake_request(method, path, payload=None, **kwargs):
        calls.append((method, path, payload))
        if method == "GET" and path.endswith("/runs?pageSize=20"):
            return {"runs": [{"runId": "manual-run", "status": "running"}]}
        if method == "GET" and path.startswith("/notebooks/"):
            return {"notebook": {"blocks": []}}
        raise AssertionError((method, path, payload))

    monkeypatch.setattr(module.dn, "_request_json", fake_request)
    result = module.maybe_auto_trigger(
        current=SimpleNamespace(batch_id="batch-1"),
        storage=EmptyStorage(),
        config={"notebook_id": "notebook-1"},
        state_path=tmp_path / "autotrigger.json",
    )

    assert result == {
        "triggered": False,
        "batch_id": "batch-1",
        "reason": "run-active",
        "run_id": "manual-run",
    }
    assert not any(method == "POST" for method, _, _ in calls)
