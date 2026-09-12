from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_engine_scripts_do_not_use_removed_official_digest_flag():
    paths = [
        ROOT / "scripts" / "install_stockfish.sh",
        ROOT / "scripts" / "setup_dev.sh",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")

        assert "--official-digest" not in text, (
            f"stale --official-digest flag remains in {path}"
        )


def test_platform_scripts_are_rootless_and_portable():
    installer = ROOT / "scripts" / "install_stockfish.sh"
    setup = ROOT / "scripts" / "setup_platform.sh"
    qualify = ROOT / "scripts" / "qualify_platform.sh"
    dev_setup = ROOT / "scripts" / "setup_dev.sh"
    devcontainer = ROOT / ".devcontainer" / "devcontainer.json"

    assert setup.exists()
    assert qualify.exists()

    install_text = installer.read_text(encoding="utf-8")
    setup_text = setup.read_text(encoding="utf-8")
    qualify_text = qualify.read_text(encoding="utf-8")
    dev_text = dev_setup.read_text(encoding="utf-8")
    container_text = devcontainer.read_text(encoding="utf-8")
    combined = "\n".join([install_text, setup_text, dev_text, container_text])

    assert "sudo " not in install_text
    assert "/usr/local/bin/stockfish" not in combined
    assert "Codespace ready" not in dev_text
    assert "setup_platform.sh" in dev_text
    assert "cgm-bench" in qualify_text
    assert "qualify" in qualify_text


def test_platform_setup_uses_project_virtualenv():
    text = (ROOT / "scripts" / "setup_platform.sh").read_text(encoding="utf-8")
    assert "-m venv" in text
    assert "CGM_VENV" in text


def test_setup_platform_falls_back_when_stdlib_venv_cannot_run_ensurepip(
    tmp_path,
):
    fake_python = tmp_path / "python3"
    call_log = tmp_path / "calls.log"
    venv = tmp_path / "venv"
    engine = tmp_path / "stockfish"

    engine.write_text("fake", encoding="utf-8")

    fake_python.write_text(
        """#!/usr/bin/env bash
set -e
printf '%s\\n' "$*" >> "$CGM_TEST_CALL_LOG"

if [[ "${1:-}" == "-" ]]; then
    cat >/dev/null
    exit 0
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
    if [[ " $* " != *" --without-pip "* ]]; then
        exit 1
    fi

    target="${@: -1}"
    mkdir -p "$target/bin"

    cat > "$target/bin/python" <<'PYBIN'
#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then
    echo "Python 3.12.13"
fi
exit 0
PYBIN
    chmod +x "$target/bin/python"

    cat > "$target/bin/cgm-engine" <<'ENGBIN'
#!/usr/bin/env bash
if [[ "${1:-}" == "install-path" ]]; then
    echo "$CGM_ENGINE_INSTALL_PATH"
    exit 0
fi
if [[ "${1:-}" == "verify" ]]; then
    exit 0
fi
exit 1
ENGBIN
    chmod +x "$target/bin/cgm-engine"
    exit 0
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "pip" && "${3:-}" == "--python" ]]; then
    exit 0
fi

exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "CGM_BOOTSTRAP_PYTHON": str(fake_python),
            "CGM_VENV": str(venv),
            "CGM_ENGINE_INSTALL_PATH": str(engine),
            "CGM_TEST_CALL_LOG": str(call_log),
        }
    )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "setup_platform.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )

    calls = call_log.read_text(encoding="utf-8")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "-m venv" in calls
    assert "--without-pip" in calls
    assert "-m pip --python" in calls
