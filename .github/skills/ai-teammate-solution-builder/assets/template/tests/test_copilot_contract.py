from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_optional_copilot_runtime_uses_verified_sdk_contract():
    path = ROOT / "app" / "copilot_runtime.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    constructors = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "CopilotClient"
    ]
    assert len(constructors) == 1
    assert "github_token" in {keyword.arg for keyword in constructors[0].keywords}
    assert "enable_skills=True" in source
    assert "skill_directories=" in source
    assert "send_and_wait" in source
    assert "PermissionHandler.approve_all" in source
    assert "if not config.DEVELOPMENT_MODE" in source
