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
        3. Upload a PGN with Molab's file browser and paste its path below.
        4. Click **Analyze PGN**. Molab uses the provider policy of
           **4 workers**, Threads=1 per worker, depth=18. Results are written
           under `cgm_molab_data/db` and `cgm_molab_data/output`.

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
            "hash_mb_per_worker": 256,
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
        [str(_analyzer), "--workers", "4", str(_input)],
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
        - Workers: `4`
        - Threads per worker: `1`
        - Depth: `18`

        **SQLite database(s)**
        {_db_lines or '- none'}

        **PGN output(s)**
        {_output_lines or '- none'}

        Download/copy important generated files before the Molab runtime is
        discarded, or save them to remote storage such as S3.
        """
    )
    return (analysis_result,)


if __name__ == "__main__":
    app.run()
