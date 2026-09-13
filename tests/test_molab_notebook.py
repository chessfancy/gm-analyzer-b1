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


def test_molab_platform_probe_reuses_verified_engine_and_exports_json():
    text = NOTEBOOK.read_text(encoding="utf-8")

    assert "Export platform JSON" in text
    assert 'setup_status["engine"]' in text
    assert 'CGM_STOCKFISH' in text
    assert "molab_platform.json" in text
    assert '"hash_mb_per_worker": 1536' in text
    assert "hardware_info" in text
    assert "suggested_workers" in text


def test_molab_analysis_uses_provider_worker_policy():
    text = NOTEBOOK.read_text(encoding="utf-8")

    assert '"--platform-profile", "molab"' in text


def test_molab_analysis_uses_depth_19_hash_and_snapshot_policy():
    text = NOTEBOOK.read_text(encoding="utf-8")
    tree = ast.parse(text)

    def argument_value(node):
        if isinstance(node, ast.Constant):
            return node.value
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "str"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
        ):
            return f"str({node.args[0].id})"
        return None

    expected_command = [
        "str(_analyzer)",
        "--platform-profile",
        "molab",
        "--depth",
        "19",
        "str(_input)",
    ]
    matching_commands = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "run"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            continue
        command = [argument_value(element) for element in node.args[0].elts]
        if command == expected_command:
            matching_commands.append(command)

    assert matching_commands == [expected_command]
    assert "Workers: 4" in text
    assert "Threads/worker: 1" in text
    assert "Hash/worker: 1536 MB" in text
    assert "Depth: 19" in text
    assert "Time limit: OFF" in text
    assert "Snapshot depths: 12, 14, 16, 18, 19" in text
