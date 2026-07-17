from __future__ import annotations

import json
import struct
import importlib.util
from pathlib import Path

import pytest
import yaml

from app import config
from app.capabilities import CapabilityRegistry
from app.data import DataCatalog
from app.mcp_server import build_server
from app.state import SQLiteStateStore


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_fastmcp_surface_matches_shared_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MCP_ALLOW_DEV_NO_AUTH", True)
    registry = CapabilityRegistry(
        config.SPEC, DataCatalog(config.SPEC), SQLiteStateStore(tmp_path / "mcp.db")
    )
    server = build_server(registry)
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        tool.name for tool in registry.tool_specs("mcp")
    }
    by_name = {tool.name: tool for tool in tools}
    ui_tool_names = {
        resource.tool_name
        for resource in config.SPEC.user_interfaces.resources
        if "mcp" in resource.surfaces
    }
    for name, tool in by_name.items():
        if name not in ui_tool_names and name != "resolve_reviews":
            assert tool.meta["ui"]["visibility"] == ["model"]
    for resource in config.SPEC.user_interfaces.resources:
        if "mcp" not in resource.surfaces:
            continue
        metadata = by_name[resource.tool_name].meta
        assert metadata["ui"]["resourceUri"] == resource.resource_uri
        assert metadata["ui"]["visibility"] == ["model", "app"]
        content = list(await server.read_resource(resource.resource_uri))
        assert len(content) == 1
        assert content[0].mime_type == "text/html;profile=mcp-app"
        assert "tools/call" in content[0].content
    if "resolve_reviews" in by_name:
        assert by_name["resolve_reviews"].meta["ui"]["visibility"] == ["app"]
        assert "resolve_reviews" not in {
            tool.name for tool in registry.tool_specs("agent")
        }
    for capability in config.SPEC.capabilities:
        if capability.side_effect and "mcp" in capability.expose:
            assert "idempotency_key" in by_name[capability.id].inputSchema["required"]
    for workflow in config.SPEC.workflows:
        assert "idempotency_key" in by_name[f"start_{workflow.id}"].inputSchema["required"]


def test_agent365_manifest_templates_and_icons_are_consistent():
    manifest = json.loads((ROOT / "manifest" / "manifest.json").read_text())
    template = json.loads(
        (ROOT / "manifest" / "agenticUserTemplateManifest.json").read_text()
    )
    assert manifest["agenticUserTemplates"][0]["id"] == template["id"]
    assert manifest["id"] == template["agentIdentityBlueprintId"]
    assert manifest["id"] == "${A365_BLUEPRINT_APP_ID}"
    assert template["id"] == "${A365_AGENTIC_TEMPLATE_ID}"
    assert template["communicationProtocol"] == "activityProtocol"
    for filename, expected in (("color.png", 192), ("outline.png", 32)):
        content = (ROOT / "manifest" / filename).read_bytes()
        assert content[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", content[16:24]) == (expected, expected)


def test_tabs_only_teams_host_uses_a_fresh_catalog_id_and_a365_sso():
    manifest = json.loads((ROOT / "appPackage" / "manifest.json").read_text())
    lifecycle = yaml.safe_load((ROOT / "m365agents.yml").read_text())
    assert manifest["manifestVersion"] == "1.26"
    assert manifest["id"] == "${{TEAMS_APP_ID}}"
    assert manifest["webApplicationInfo"]["id"] == "${{A365_BLUEPRINT_APP_ID}}"
    assert manifest["webApplicationInfo"]["resource"] == (
        "api://${{CONTROL_PLANE_DOMAIN}}/${{A365_BLUEPRINT_APP_ID}}"
    )
    assert manifest["permissions"] == ["identity"]
    assert "bots" not in manifest
    assert {tab["contentUrl"] for tab in manifest["staticTabs"]} == {
        "https://${{CONTROL_PLANE_DOMAIN}}" + tab.path
        for tab in config.SPEC.teams_app.tabs
    }
    actions = lifecycle["provision"] + lifecycle["publish"]
    assert lifecycle["provision"][0]["uses"] == "teamsApp/create"
    assert not any(
        action["uses"].startswith(("aadApp/", "botAadApp/", "botFramework/"))
        for action in actions
    )
    assert any(action["uses"] == "script" and "render_teams_manifest.py" in action["with"]["run"] for action in actions)


def test_manifest_renderer_requires_real_blueprint_id(monkeypatch, tmp_path):
    path = ROOT / "scripts" / "render_agent365_manifest.py"
    spec = importlib.util.spec_from_file_location("manifest_renderer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("CONTROL_PLANE_PUBLIC_URL", "https://control.example.com")
    monkeypatch.delenv("A365_BLUEPRINT_APP_ID", raising=False)
    with pytest.raises(ValueError, match="A365_BLUEPRINT_APP_ID"):
        module.render()


def test_a365_and_byo_registration_are_safe_and_complete():
    a365 = json.loads((ROOT / "a365.config.json").read_text())
    registration = json.loads(
        (ROOT / "scripts" / "byo-mcp-registration.template.json").read_text()
    )
    assert a365["tenantId"] == ""
    assert a365["clientAppId"] == ""
    assert a365["aiTeammate"] is True
    assert registration["authType"] == "EntraOAuth"
    assert registration["serverUrl"] == "${MCP__PUBLIC_URL}"
    expected = {
        capability.id
        for capability in config.SPEC.capabilities
        if "mcp" in capability.expose
    } | {"get_skill"} | {f"start_{workflow.id}" for workflow in config.SPEC.workflows} | {
        resource.tool_name
        for resource in config.SPEC.user_interfaces.resources
        if "mcp" in resource.surfaces
    }
    if any(
        resource.kind == "hitl" and "mcp" in resource.surfaces
        for resource in config.SPEC.user_interfaces.resources
    ):
        expected.add("resolve_reviews")
    assert {tool["name"] for tool in registration["tools"]} == expected


def test_generated_skills_match_spec():
    for skill in config.SPEC.skills:
        path = ROOT / "app" / "generated_skills" / skill.id / "SKILL.md"
        content = path.read_text(encoding="utf-8")
        assert f"name: {skill.id}" in content
        assert skill.instructions in content
        assert all(f"start_{workflow}" in content for workflow in skill.workflows)
        reviewed = {
            item.id
            for item in config.SPEC.capabilities
            if item.review_mode != "none"
        }
        frontmatter = content.split("---", 2)[1]
        assert not any(capability in frontmatter for capability in reviewed)


def test_bicep_uses_federated_identity_and_keyless_shared_state():
    content = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
    assert "allowSharedKeyAccess: false" in content
    assert "Storage Table Data Contributor" not in content  # role is identified by immutable GUID
    assert "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3" in content
    assert "STATE_TABLE_ENDPOINT" in content
    assert "FederatedCredentials" in content
    assert "FEDERATEDCLIENTID" in content
    assert "ENABLE_A365_OBSERVABILITY_EXPORTER" in content
    assert "MCP_ALLOW_DEV_NO_AUTH', value: 'false'" in content
    assert "AccountKey" not in content
    assert "SharedAccessSignature" not in content
