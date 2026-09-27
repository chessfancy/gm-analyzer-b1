from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts" / "oracle" / "refill_backfill_queue.py"
WRAPPER = ROOT / "scripts" / "oracle" / "run_backfill_refill.sh"


def test_refill_cli_exposes_safe_production_defaults():
    proc = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    text = proc.stdout
    assert "--low-watermark" in text
    assert "--high-watermark" in text
    assert "--target-plies" in text
    assert "--priority" in text
    assert "--source" in text

    source = CLI.read_text(encoding="utf-8")
    assert "default=8" in source
    assert "default=24" in source
    assert "default=1800" in source
    assert "default=500" in source
    assert 'default="chess-results"' in source


def test_refill_wrapper_is_lock_protected_and_calls_cli():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "flock -n" in text
    assert "refill_backfill_queue.py" in text
    assert "PYTHONPATH=src" in text
