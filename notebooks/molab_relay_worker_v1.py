import marimo

__generated_with = "0.23.6"
app = marimo.App(width="full")


@app.cell
def _():
    import os
    import shutil
    import subprocess
    from pathlib import Path

    import marimo as mo

    return Path, mo, os, shutil, subprocess


@app.cell
def _(Path):
    repo_url = "https://github.com/chessfancy/gm-analyzer-b1.git"
    branch = "chatgpt-work"
    scratch_root = Path("/tmp/chessgrandmaster-molab-relay")
    repo_dir = scratch_root / "repo"
    venv_dir = scratch_root / "venv"
    data_root = (Path.cwd() / "cgm_molab_relay_data").resolve()
    return branch, data_root, repo_dir, repo_url, scratch_root, venv_dir


@app.cell
def _(mo):
    mo.md(
        """
        # ChessGrandmaster B1 — Molab HTTPS relay worker

        This cache-busting worker notebook is intentionally small.

        1. Upload the relay credential with Relay credential.
        2. Click Setup / Reuse B1.
        3. Click Run Oracle relay batch.

        The credential is kept only in the live UI value and passed to the worker
        process through its environment. It is not written into notebook source.
        Completed result shards and progress are uploaded to
        https://mo.chessfancy.com/v1 as they finish.
        """
    )
    return


@app.cell
def _(mo):
    relay_credential = mo.ui.file(
        label="Relay credential",
        multiple=False,
        kind="button",
        max_size=4096,
    )
    relay_credential
    return (relay_credential,)


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
        mo.md("Click Setup / Reuse B1 to prepare Molab."),
    )

    if shutil.which("git") is None:
        raise RuntimeError("git is required but was not found in the Molab runtime")

    scratch_root.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)

    if (repo_dir / ".git").is_dir():
        subprocess.run(["git", "fetch", "origin", branch], cwd=repo_dir, check=True)
        subprocess.run(["git", "checkout", branch], cwd=repo_dir, check=True)
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

        - Repo: {setup_status['repo']}
        - Branch: {setup_status['branch']}
        - Venv: {setup_status['venv']}
        - CGM_HOME: {setup_status['home']}
        - Stockfish: {setup_status['engine']}
        """
    )
    return (setup_status,)


@app.cell
def _(mo, setup_status):
    run_button = mo.ui.run_button(label="Run Oracle relay batch", kind="success")
    mo.vstack(
        [
            mo.md(
                f"""
                ## Oracle coordinator — HTTPS relay

                Setup is ready with Stockfish {setup_status['engine']}.
                Upload the relay credential, then run the current Oracle batch.
                """
            ),
            run_button,
        ]
    )
    return (run_button,)


@app.cell
def _(
    data_root,
    mo,
    os,
    relay_credential,
    repo_dir,
    run_button,
    setup_status,
    subprocess,
    venv_dir,
):
    mo.stop(
        not run_button.value,
        mo.md("Click Run Oracle relay batch after setup."),
    )

    _credential_bytes = relay_credential.contents()
    if not _credential_bytes:
        raise RuntimeError("Relay credential file has not been uploaded")
    _token = _credential_bytes.decode("utf-8").strip()
    if not _token:
        raise RuntimeError("Relay credential is empty")

    _env = os.environ.copy()
    _env["CGM_MOLAB_RELAY_URL"] = "https://mo.chessfancy.com/v1"
    _env["CGM_MOLAB_RELAY_TOKEN"] = _token
    _env["CGM_VENV"] = str(venv_dir)
    _env["CGM_HOME"] = str(data_root)
    _env["CGM_STOCKFISH"] = setup_status["engine"]

    _runner = repo_dir / "scripts" / "workers" / "run_molab_http_cycle.py"
    _work_root = data_root / "http-worker"
    subprocess.run(
        [
            str(venv_dir / "bin" / "python"),
            str(_runner),
            "--work-root",
            str(_work_root),
        ],
        cwd=repo_dir,
        env=_env,
        check=True,
    )
    _token = ""
    mo.md(
        """
        ## Relay batch complete

        Validated result shards and ready.json are already on
        mo.chessfancy.com. Oracle will verify and import them automatically.
        """
    )
    return


if __name__ == "__main__":
    app.run()
