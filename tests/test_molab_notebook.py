import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "molab_b1.py"


def test_molab_notebook_is_portable_b1_entrypoint():
    text = NOTEBOOK.read_text(encoding="utf-8")
    ast.parse(text)

    assert "marimo.App" in text
    assert "mo.ui.run_button" in text
    assert "chatgpt-work" in text
    assert "scripts/setup_platform.sh" in text
    assert "cgm-engine" in text and "verify" in text
    assert "cgm-analyze" in text
    assert "CGM_VENV" in text
    assert "CGM_HOME" in text
    assert "cgm-bench" not in text
