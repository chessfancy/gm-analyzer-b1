from pathlib import Path


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
