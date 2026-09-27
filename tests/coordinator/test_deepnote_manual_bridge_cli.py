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
