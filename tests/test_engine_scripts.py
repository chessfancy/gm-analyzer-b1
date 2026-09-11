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
