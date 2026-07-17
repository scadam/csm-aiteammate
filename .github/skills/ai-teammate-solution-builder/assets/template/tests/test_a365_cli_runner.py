from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "scripts" / "provision_agent365.py"
    spec = importlib.util.spec_from_file_location("provision_agent365", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_temp_state(module, tmp_path):
    module.STATE_DIR = tmp_path / ".a365"
    module.STATE_FILE = module.STATE_DIR / "provisioning-state.json"
    module.SAFE_ENV_FILE = module.STATE_DIR / "runtime-values.env"


def test_early_cli_flow_order_and_safe_state(monkeypatch, tmp_path):
    module = _module()
    _set_temp_state(module, tmp_path)
    commands = []

    def fake_run(step, command, state):
        commands.append(command)
        state["steps"][step] = {
            "status": "complete",
            "exitCode": 0,
            "adminConsentUrls": [],
            "updatedAt": module._now(),
        }
        module._save(state)
        return "complete"

    monkeypatch.setattr(module.shutil, "which", lambda _name: "a365")
    monkeypatch.setattr(module, "_run", fake_run)
    args = argparse.Namespace(agent_name="Runner Agent", tenant_id="tenant", dry_run=False)
    assert module.early(args) == 0
    assert [command[:4] for command in commands] == [
        ["a365", "setup", "requirements"],
        ["a365", "setup", "blueprint", "--agent-name"],
        ["a365", "setup", "permissions", "mcp"],
        ["a365", "setup", "permissions", "bot"],
    ]
    assert "--no-endpoint" in commands[1]
    assert "--m365" in commands[1]
    content = module.STATE_FILE.read_text(encoding="utf-8")
    assert "clientSecret" not in content


def test_admin_consent_is_a_resumable_checkpoint(monkeypatch, tmp_path):
    module = _module()
    _set_temp_state(module, tmp_path)

    def fake_run(step, _command, state):
        status = "admin_action_required" if step == "permissions_mcp" else "complete"
        state["steps"][step] = {
            "status": status,
            "exitCode": 0,
            "adminConsentUrls": (
                ["https://login.microsoftonline.com/tenant/v2.0/adminconsent?client_id=x"]
                if status == "admin_action_required"
                else []
            ),
            "updatedAt": module._now(),
        }
        module._save(state)
        return status

    monkeypatch.setattr(module.shutil, "which", lambda _name: "a365")
    monkeypatch.setattr(module, "_run", fake_run)
    args = argparse.Namespace(agent_name="Runner Agent", tenant_id="tenant", dry_run=False)
    assert module.early(args) == 2
    state = json.loads(module.STATE_FILE.read_text(encoding="utf-8"))
    assert state["steps"]["permissions_mcp"]["status"] == "admin_action_required"
    assert state["steps"]["permissions_mcp"]["adminConsentUrls"]


def test_sanitizer_never_returns_secret_values():
    module = _module()
    assert module._sanitize("clientSecret: abc123") == "clientSecret: [REDACTED]"
    assert module._sanitize("access_token=abc123") == "access_token: [REDACTED]"


def test_safe_a365_outputs_feed_the_teams_host_environment(tmp_path):
    module = _module()
    _set_temp_state(module, tmp_path)
    module.TEAMS_ENV_FILE = tmp_path / "env" / ".env.dev"
    module.TEAMS_ENV_FILE.parent.mkdir(parents=True)
    module.TEAMS_ENV_FILE.write_text(
        "TEAMS_APP_NAME=Example Control Plane\nTEAMS_APP_ID=\nA365_BLUEPRINT_APP_ID=\nCONTROL_PLANE_DOMAIN=\n"
    )
    blueprint = "11111111-1111-4111-8111-111111111111"
    module._update_teams_environment(
        blueprint_id=blueprint,
        control_plane_url="https://control.example.com",
    )
    content = module.TEAMS_ENV_FILE.read_text()
    assert f"A365_BLUEPRINT_APP_ID={blueprint}" in content
    assert "CONTROL_PLANE_DOMAIN=control.example.com" in content
    assert "TEAMS_APP_ID=" in content


def test_endpoint_and_teams_publish_must_share_one_public_origin(monkeypatch, tmp_path):
    module = _module()
    _set_temp_state(module, tmp_path)
    module.TEAMS_ENV_FILE = tmp_path / "env" / ".env.dev"
    module.TEAMS_ENV_FILE.parent.mkdir(parents=True)
    module.TEAMS_ENV_FILE.write_text("A365_BLUEPRINT_APP_ID=\nCONTROL_PLANE_DOMAIN=\n")
    monkeypatch.setattr(module, "_load_spec", lambda: {"solution": {"name": "Example"}})

    def fake_run(step, _command, state):
        state["steps"][step] = {"status": "complete"}
        return "complete"

    monkeypatch.setattr(module, "_run", fake_run)
    endpoint_args = argparse.Namespace(
        url="https://shared.example.com/api/messages",
        agent_name="Example",
        tenant_id="tenant",
        dry_run=False,
    )
    assert module.endpoint(endpoint_args) == 0
    state = json.loads(module.STATE_FILE.read_text())
    state["safeOutputs"]["agentBlueprintId"] = "11111111-1111-4111-8111-111111111111"
    module._save(state)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="same public origin"):
        module.publish(
            argparse.Namespace(
                control_plane_url="https://different.example.com",
                agent_name="Example",
                tenant_id="tenant",
                dry_run=False,
            )
        )
