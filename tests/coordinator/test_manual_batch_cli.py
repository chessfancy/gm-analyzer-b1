from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORACLE_CLI = ROOT / "scripts" / "oracle" / "manual_batch.py"
WORKER_CLI = ROOT / "scripts" / "workers" / "run_manual_batch.py"
NOTEBOOK = ROOT / "notebooks" / "molab_b1.py"


def test_oracle_manual_batch_cli_has_create_and_import_commands():
    proc = subprocess.run([sys.executable, str(ORACLE_CLI), "--help"], cwd=ROOT, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    assert "create" in proc.stdout
    assert "import" in proc.stdout
    source = ORACLE_CLI.read_text(encoding="utf-8")
    assert "molab-batch.zip" in source
    assert "deepnote-batch.zip" in source
    assert 'PUBLISH_ROOT = CGM / "distribution"' in source


def test_worker_manual_batch_cli_packages_one_return_archive():
    proc = subprocess.run([sys.executable, str(WORKER_CLI), "--help"], cwd=ROOT, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    assert "--batch-archive" in proc.stdout
    assert "--archive" in proc.stdout


def test_molab_notebook_exposes_coordinator_batch_return_flow():
    text = NOTEBOOK.read_text(encoding="utf-8")
    assert "Download Oracle coordinator batch" in text
    assert "http://149.118.50.253/molab-batch.zip" in text
    assert "run_manual_batch.py" in text
    assert "manual-results.zip" in text
    assert "manual-results.zip.sha256" in text


def test_manual_worker_can_load_job_executor_from_subprocess_environment():
    import os

    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    code = (
        "import runpy; "
        "ns=runpy.run_path('scripts/workers/run_manual_batch.py'); "
        "fn=ns['_load_job_executor'](); "
        "print(fn.__name__)"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "execute_job"


def test_oracle_has_molab_s3_bridge_runner_and_secret_setup():
    bridge = ROOT / "scripts" / "oracle" / "run_molab_s3_bridge.py"
    wrapper = ROOT / "scripts" / "oracle" / "run_molab_s3_bridge.sh"
    secrets = ROOT / "scripts" / "oracle" / "setup_molab_s3_secrets.sh"
    worker = ROOT / "scripts" / "workers" / "run_molab_s3_cycle.py"
    probe = ROOT / "scripts" / "oracle" / "probe_molab_s3.py"
    for path in (bridge, wrapper, secrets, worker, probe):
        assert path.is_file(), path
    assert "CGM_MOLAB_S3_ENDPOINT" in bridge.read_text(encoding="utf-8")
    assert "molab_s3.env" in wrapper.read_text(encoding="utf-8")
    assert "read -rs" in secrets.read_text(encoding="utf-8")
