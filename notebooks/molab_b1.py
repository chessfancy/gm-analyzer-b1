import marimo

__generated_with = "0.23.6"
app = marimo.App(width="full")


@app.cell
def _():
    import json
    import os
    import shutil
    import subprocess
    from datetime import datetime, timezone
    from pathlib import Path

    import marimo as mo

    return Path, datetime, json, mo, os, shutil, subprocess, timezone


@app.cell
def _(Path):
    repo_url = "https://github.com/chessfancy/gm-analyzer-b1.git"
    branch = "chatgpt-work"
    scratch_root = Path("/tmp/chessgrandmaster-molab")
    repo_dir = scratch_root / "repo"
    venv_dir = scratch_root / "venv"
    data_root = (Path.cwd() / "cgm_molab_data").resolve()
    return branch, data_root, repo_dir, repo_url, scratch_root, venv_dir


@app.cell
def _(mo):
    mo.md(
        """
        # ChessGrandmaster B1 on Molab

        Portable B1 runner for the same GitHub code used on Codespaces,
        Deepnote and Kaggle. This notebook does **not** run benchmarks.

        1. Click **Setup / Reuse B1** to clone/update the repo, install the
           package, install and verify the pinned Stockfish 19 binary.
        2. Optionally click **Export platform JSON** and upload
           `cgm_molab_data/molab_platform.json` for worker selection.
        3. For coordinator work, use **Run next Oracle S3 batch**: one click
           downloads the current immutable batch, resumes completed shards from S3,
           runs B1, uploads each validated shard plus heartbeat, and publishes ready.
           The older manual download/upload controls remain as a fallback.
        4. For ad-hoc PGNs, click **Analyze PGN**. Molab uses the provider policy of
           **4 workers**, Threads/worker=1, Hash/worker=1536 MB, depth=19,
           no time limit, and snapshots at depths 12, 14, 16, 18 and 19.
           Results are written under `cgm_molab_data/db` and
           `cgm_molab_data/output`.

        Molab treats `/tmp` as scratch runtime. A fresh runtime needs setup
        again, while files uploaded through Molab's file browser can persist
        across sessions. Keep important result files by downloading them or
        copying them to persistent/remote storage.
        """
    )
    return


@app.cell
def _(mo):
    setup_button = mo.ui.run_button(label="Setup / Reuse B1", kind="success")
    setup_button
    return (setup_button,)


@app.cell
def _(
    branch,
    data_root,
    mo,
    os,
    repo_dir,
    repo_url,
    scratch_root,
    setup_button,
    shutil,
    subprocess,
    venv_dir,
):
    mo.stop(
        not setup_button.value,
        mo.md("Click **Setup / Reuse B1** to prepare Molab."),
    )

    if shutil.which("git") is None:
        raise RuntimeError("git is required but was not found in the Molab runtime")

    scratch_root.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    if (repo_dir / ".git").is_dir():
        subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=repo_dir,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", branch],
            cwd=repo_dir,
            check=True,
        )
        subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=repo_dir,
            check=True,
        )
    else:
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        subprocess.run(
            [
                "git",
                "clone",
                "--branch",
                branch,
                "--single-branch",
                repo_url,
                str(repo_dir),
            ],
            check=True,
        )

    _env = os.environ.copy()
    _env["CGM_VENV"] = str(venv_dir)
    _env["CGM_HOME"] = str(data_root)
    _env["CGM_INSTALL_S3"] = "1"

    subprocess.run(
        ["bash", "scripts/setup_platform.sh"],
        cwd=repo_dir,
        env=_env,
        check=True,
    )

    _engine_cli = venv_dir / "bin" / "cgm-engine"
    _engine_path = subprocess.check_output(
        [str(_engine_cli), "install-path"],
        cwd=repo_dir,
        env=_env,
        text=True,
    ).strip()

    subprocess.run(
        [str(_engine_cli), "verify", _engine_path],
        cwd=repo_dir,
        env=_env,
        check=True,
    )

    setup_status = {
        "repo": str(repo_dir),
        "branch": branch,
        "venv": str(venv_dir),
        "home": str(data_root),
        "engine": _engine_path,
    }

    mo.md(
        f"""
        ## Setup ready

        - Repo: `{setup_status['repo']}`
        - Branch: `{setup_status['branch']}`
        - Venv: `{setup_status['venv']}`
        - CGM_HOME: `{setup_status['home']}`
        - Stockfish: `{setup_status['engine']}`
        """
    )
    return (setup_status,)


@app.cell
def _(mo, setup_status):
    platform_button = mo.ui.run_button(
        label="Export platform JSON",
        kind="neutral",
    )
    mo.vstack(
        [
            mo.md(
                f"Verified Stockfish: `{setup_status['engine']}`\n\n"
                "This probe reads CPU quota, effective CPU, RAM and the "
                "suggested worker count. It does not benchmark Stockfish."
            ),
            platform_button,
        ]
    )
    return (platform_button,)


@app.cell
def _(
    data_root,
    datetime,
    json,
    mo,
    os,
    platform_button,
    repo_dir,
    setup_status,
    subprocess,
    timezone,
    venv_dir,
):
    mo.stop(
        not platform_button.value,
        mo.md("Click **Export platform JSON** after setup."),
    )

    _engine_path = setup_status["engine"]
    _env = os.environ.copy()
    _env["CGM_VENV"] = str(venv_dir)
    _env["CGM_HOME"] = str(data_root)
    _env["CGM_STOCKFISH"] = _engine_path

    _probe_code = r'''
import json
import sys

from chessgrandmaster.benchmark import hardware_info
from chessgrandmaster.engine_manifest import verify_engine_binary

engine_path = sys.argv[1]

print(json.dumps({
    "hardware": hardware_info(),
    "engine": verify_engine_binary(engine_path),
}))
'''

    _runtime = json.loads(
        subprocess.check_output(
            [
                str(venv_dir / "bin" / "python"),
                "-c",
                _probe_code,
                _engine_path,
            ],
            cwd=repo_dir,
            env=_env,
            text=True,
        )
    )

    _commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        text=True,
    ).strip()

    _report = {
        "schema_version": "cgm-platform-probe-1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "platform_label": "molab",
        "git": {
            "repository": "chessfancy/gm-analyzer-b1",
            "branch": setup_status["branch"],
            "commit": _commit,
        },
        "hardware": _runtime["hardware"],
        "engine": _runtime["engine"],
        "worker_policy": {
            "threads_per_worker": 1,
            "hash_mb_per_worker": 1536,
            "suggested_workers": _runtime["hardware"]["suggested_workers"],
        },
    }

    _report_path = data_root / "molab_platform.json"
    _report_path.write_text(
        json.dumps(
            _report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    platform_report_path = str(_report_path)

    mo.md(
        f"""
        ## Molab platform probe ready

        - Logical CPUs: `{_report['hardware']['logical_cpu']}`
        - CPU quota: `{_report['hardware']['cgroup_cpu_quota']}`
        - Effective CPU: `{_report['hardware']['effective_cpu']}`
        - Suggested workers: `{_report['hardware']['suggested_workers']}`
        - Memory GiB: `{_report['hardware']['memory_gib']}`
        - JSON: `{platform_report_path}`

        Download `molab_platform.json` from the Molab file browser and upload
        it to ChatGPT for comparison with Codespaces, Deepnote and Kaggle.
        """
    )
    return (platform_report_path,)


@app.cell
def _(mo):
    coordinator_batch_url = mo.ui.text(
        label="Oracle coordinator batch URL",
        value="http://149.118.50.253/molab-batch.zip",
        full_width=True,
    )
    coordinator_fetch_button = mo.ui.run_button(
        label="Download Oracle coordinator batch",
        kind="success",
    )
    mo.vstack([coordinator_batch_url, coordinator_fetch_button])
    return coordinator_batch_url, coordinator_fetch_button


@app.cell
def _(coordinator_batch_url, coordinator_fetch_button, data_root, mo):
    mo.stop(
        not coordinator_fetch_button.value,
        mo.md("Click **Download Oracle coordinator batch** to fetch immutable job bundles."),
    )
    import hashlib
    import urllib.request

    _url = coordinator_batch_url.value.strip()
    if not _url.startswith(("http://", "https://")):
        raise ValueError("Oracle batch URL must use http:// or https://")
    _dest = data_root / "manual-batch.zip"
    _tmp = data_root / ".manual-batch.zip.download"
    _tmp.unlink(missing_ok=True)
    _hash = hashlib.sha256()
    with urllib.request.urlopen(_url, timeout=120) as _response, _tmp.open("wb") as _out:
        while True:
            _chunk = _response.read(1024 * 1024)
            if not _chunk:
                break
            _out.write(_chunk)
            _hash.update(_chunk)
    _actual = _hash.hexdigest()
    with urllib.request.urlopen(_url + ".sha256", timeout=30) as _response:
        _expected = _response.read().decode("utf-8").strip().split()[0].lower()
    if _actual != _expected:
        _tmp.unlink(missing_ok=True)
        raise RuntimeError(f"SHA256 mismatch: expected {_expected}, got {_actual}")
    _tmp.replace(_dest)
    coordinator_batch_download = {"path": str(_dest), "sha256": _actual}
    mo.md(f"## Coordinator batch ready\n- `{_dest}`\n- SHA-256 verified: `{_actual}`")
    return (coordinator_batch_download,)


@app.cell
def _(coordinator_batch_download, mo):
    coordinator_run_button = mo.ui.run_button(
        label="Run coordinator batch + package results",
        kind="success",
    )
    mo.stop(not coordinator_batch_download, mo.md("Download a coordinator batch first."))
    coordinator_run_button
    return (coordinator_run_button,)


@app.cell
def _(coordinator_batch_download, coordinator_run_button, data_root, mo, os, repo_dir, setup_status, subprocess, venv_dir):
    mo.stop(
        not coordinator_run_button.value,
        mo.md("Click **Run coordinator batch + package results** when ready."),
    )
    _runner = repo_dir / "scripts" / "workers" / "run_manual_batch.py"
    _python = venv_dir / "bin" / "python"
    _result_dir = data_root / "manual-results"
    _archive = data_root / "manual-results.zip"
    _env = os.environ.copy()
    _env["CGM_VENV"] = str(venv_dir)
    _env["CGM_HOME"] = str(data_root)
    _env["CGM_STOCKFISH"] = setup_status["engine"]
    _proc = subprocess.run(
        [str(_python), str(_runner), "--batch-archive", coordinator_batch_download["path"],
         "--result", str(_result_dir), "--archive", str(_archive)],
        cwd=repo_dir, env=_env, text=True, capture_output=True, check=True,
    )
    coordinator_result = json.loads(_proc.stdout.strip().splitlines()[-1])
    _sha_path = data_root / "manual-results.zip.sha256"
    if not _archive.is_file() or not _sha_path.is_file():
        raise RuntimeError("manual result archive was not created")
    mo.md(
        f"""## Coordinator result package ready

- Results: `{_archive}`
- Checksum: `{_sha_path}`
- Jobs executed: `{coordinator_result['executed']}`
- Jobs resumed/skipped: `{coordinator_result['skipped']}`

Download **both** files and copy the ZIP back to Oracle; Oracle will verify every inner job before marking it complete.
"""
    )
    return (coordinator_result,)


@app.cell
def _(data_root, mo):
    workload_url = mo.ui.text(
        label="Oracle workload URL",
        value="http://149.118.50.253/molab-workload.pgn",
        full_width=True,
    )
    fetch_button = mo.ui.run_button(
        label="Download Oracle workload",
        kind="success",
    )
    mo.vstack([workload_url, fetch_button])
    return fetch_button, workload_url


@app.cell
def _(data_root, fetch_button, mo, workload_url):
    mo.stop(
        not fetch_button.value,
        mo.md("Click **Download Oracle workload** to fetch the current assignment."),
    )

    import hashlib
    import urllib.request

    _url = workload_url.value.strip()
    if not _url.startswith(("http://", "https://")):
        raise ValueError("Oracle workload URL must use http:// or https://")
    data_root.mkdir(parents=True, exist_ok=True)
    _dest = data_root / "input.pgn"
    _tmp = data_root / ".input.pgn.download"
    _tmp.unlink(missing_ok=True)

    _hash = hashlib.sha256()
    _bytes = 0
    with urllib.request.urlopen(_url, timeout=120) as _response, _tmp.open("wb") as _out:
        while True:
            _chunk = _response.read(1024 * 1024)
            if not _chunk:
                break
            _out.write(_chunk)
            _hash.update(_chunk)
            _bytes += len(_chunk)

    _actual_sha = _hash.hexdigest()
    _expected_sha = None
    try:
        with urllib.request.urlopen(_url + ".sha256", timeout=30) as _response:
            _checksum_text = _response.read().decode("utf-8").strip()
        if _checksum_text:
            _expected_sha = _checksum_text.split()[0].lower()
    except Exception:
        _expected_sha = None

    if _expected_sha and _actual_sha != _expected_sha:
        _tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"SHA256 mismatch: expected {_expected_sha}, got {_actual_sha}"
        )
    _tmp.replace(_dest)

    _manifest_path = data_root / "molab-workload.json"
    _manifest_url = (
        _url[:-4] + ".json" if _url.lower().endswith(".pgn") else _url + ".json"
    )
    _manifest_status = "not available"
    try:
        with urllib.request.urlopen(_manifest_url, timeout=30) as _response:
            _manifest_bytes = _response.read()
        _manifest_path.write_bytes(_manifest_bytes)
        _manifest_status = str(_manifest_path)
    except Exception:
        pass

    workload_download = {
        "url": _url,
        "path": str(_dest),
        "bytes": _bytes,
        "sha256": _actual_sha,
        "expected_sha256": _expected_sha,
        "manifest": _manifest_status,
    }

    _verified = "verified" if _expected_sha else "downloaded (checksum endpoint unavailable)"
    mo.md(
        f"""
        ## Oracle workload ready

        - File: `{workload_download['path']}`
        - Bytes: `{workload_download['bytes']}`
        - SHA-256: `{workload_download['sha256']}`
        - Integrity: **{_verified}**
        - Manifest: `{workload_download['manifest']}`
        """
    )
    return (workload_download,)


@app.cell
def _(data_root, mo):
    pgn_path = mo.ui.text(
        label="PGN path",
        value=str(data_root / "input.pgn"),
        full_width=True,
    )
    analyze_button = mo.ui.run_button(label="Analyze PGN", kind="success")
    mo.vstack([pgn_path, analyze_button])
    return analyze_button, pgn_path


@app.cell
def _(
    Path,
    analyze_button,
    data_root,
    mo,
    os,
    pgn_path,
    repo_dir,
    setup_status,
    subprocess,
    venv_dir,
):
    mo.stop(
        not analyze_button.value,
        mo.md("Choose a PGN and click **Analyze PGN**."),
    )

    _input = Path(pgn_path.value).expanduser()
    if not _input.is_absolute():
        _input = (Path.cwd() / _input).resolve()

    if not _input.is_file():
        raise FileNotFoundError(f"PGN not found: {_input}")

    _env = os.environ.copy()
    _env["CGM_VENV"] = str(venv_dir)
    _env["CGM_HOME"] = str(data_root)
    _env["CGM_STOCKFISH"] = setup_status["engine"]

    _analyzer = venv_dir / "bin" / "cgm-analyze"
    if not _analyzer.is_file():
        raise RuntimeError("B1 is not set up yet; click Setup / Reuse B1 first")

    subprocess.run(
        [
            str(_analyzer), "--platform-profile", "molab",
            "--depth",
            "19",
            str(_input),
        ],
        cwd=repo_dir,
        env=_env,
        check=True,
    )

    _db_files = sorted((data_root / "db").glob("*.sqlite"))
    _output_files = sorted((data_root / "output").glob("*.pgn"))

    analysis_result = {
        "input": str(_input),
        "databases": [str(path) for path in _db_files],
        "outputs": [str(path) for path in _output_files],
    }

    _db_lines = "\n".join(f"- `{path}`" for path in analysis_result["databases"])
    _output_lines = "\n".join(f"- `{path}`" for path in analysis_result["outputs"])

    mo.md(
        f"""
        ## B1 analysis complete

        **Input**
        - `{analysis_result['input']}`

        **Policy**
        - Workers: 4
        - Threads/worker: 1
        - Hash/worker: 1536 MB
        - Depth: 19
        - Time limit: OFF
        - Snapshot depths: 12, 14, 16, 18, 19

        **SQLite database(s)**
        {_db_lines or '- none'}

        **PGN output(s)**
        {_output_lines or '- none'}

        Download/copy important generated files before the Molab runtime is
        discarded, or save them to remote storage such as S3.
        """
    )
    return (analysis_result,)


@app.cell
def _(mo):
    s3_coordinator_button = mo.ui.run_button(
        label="Run next Oracle S3 batch",
        kind="success",
    )
    mo.vstack([
        mo.md(
            """## Oracle coordinator — S3 one-click worker

Requires Molab secrets `CGM_MOLAB_S3_ACCESS_KEY` and
`CGM_MOLAB_S3_SECRET_KEY`. The endpoint/bucket defaults can also be supplied
as secrets/environment variables. Completed shards are uploaded to S3 as they
finish, so a later session can resume them."""
        ),
        s3_coordinator_button,
    ])
    return (s3_coordinator_button,)


@app.cell
def _(data_root, mo, os, repo_dir, s3_coordinator_button, setup_status, subprocess, venv_dir):
    mo.stop(
        not s3_coordinator_button.value,
        mo.md("Click **Run next Oracle S3 batch** after setup."),
    )
    _env = os.environ.copy()
    _env.setdefault("CGM_MOLAB_S3_ENDPOINT", "https://node02.s3interdata.com:9000")
    _env.setdefault("CGM_MOLAB_S3_BUCKET", "s3-637-35690-storage")
    _env.setdefault("CGM_MOLAB_S3_PREFIX", "gm-analyzer/molab")
    _missing = [
        name for name in ("CGM_MOLAB_S3_ACCESS_KEY", "CGM_MOLAB_S3_SECRET_KEY")
        if not _env.get(name)
    ]
    if _missing:
        raise RuntimeError(
            "Missing Molab secret(s): " + ", ".join(_missing)
            + ". Add them to Molab Secrets, then run again."
        )
    _env["CGM_VENV"] = str(venv_dir)
    _env["CGM_HOME"] = str(data_root)
    _env["CGM_STOCKFISH"] = setup_status["engine"]
    _runner = repo_dir / "scripts" / "workers" / "run_molab_s3_cycle.py"
    _work_root = data_root / "s3-worker"
    subprocess.run(
        [str(venv_dir / "bin" / "python"), str(_runner), "--work-root", str(_work_root)],
        cwd=repo_dir,
        env=_env,
        check=True,
    )
    mo.md(
        """## Oracle S3 batch complete

Result shards and `ready.json` are already in S3. Oracle will checksum-verify
and import them automatically; no manual download is required."""
    )
    return


if __name__ == "__main__":
    app.run()
