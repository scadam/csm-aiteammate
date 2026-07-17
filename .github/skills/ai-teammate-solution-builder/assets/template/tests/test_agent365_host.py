from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest
import yaml
from pathlib import Path


os.environ.setdefault(
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID",
    "00000000-0000-0000-0000-000000000001",
)
os.environ.setdefault(
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET", "offline-test-secret"
)
os.environ.setdefault(
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID",
    "00000000-0000-0000-0000-000000000002",
)
os.environ.setdefault("AI_TEAMMATE_OFFLINE", "true")


agent = importlib.import_module("app.agent")
start_server = importlib.import_module("app.start_server")
SPEC = yaml.safe_load((Path(__file__).resolve().parents[1] / "solution.yaml").read_text())
DEFAULT_MANAGER = next(
    item for item in SPEC["managers"] if item["id"] == SPEC["identity"]["default_manager_id"]
)


class FakeContext:
    def __init__(self, text="hello"):
        self.activity = SimpleNamespace(
            text=text,
            conversation=SimpleNamespace(id="conversation-1"),
            from_property=SimpleNamespace(
                aad_object_id=DEFAULT_MANAGER["principal_id"], id=DEFAULT_MANAGER["principal_id"]
            ),
            recipient=SimpleNamespace(
                agentic_app_id="agent-instance", tenant_id="tenant"
            ),
        )
        self.sent = []

    async def send_activity(self, value):
        self.sent.append(value)


def test_real_sdk_singletons_and_message_route():
    from microsoft_agents.authentication.msal import MsalConnectionManager
    from microsoft_agents.hosting.aiohttp import CloudAdapter
    from microsoft_agents.hosting.core import AgentApplication, Authorization, MemoryStorage

    assert isinstance(agent.STORAGE, MemoryStorage)
    assert isinstance(agent.CONNECTION_MANAGER, MsalConnectionManager)
    assert isinstance(agent.ADAPTER, CloudAdapter)
    assert isinstance(agent.AUTHORIZATION, Authorization)
    assert isinstance(agent.AGENT_APP, AgentApplication)
    server = start_server.create_server(
        agent.AGENT_APP,
        agent.CONNECTION_MANAGER.get_default_connection_configuration(),
    )
    assert "/api/messages" in {route.resource.canonical for route in server.router.routes()}


def test_shared_public_host_proxy_is_fixed_and_does_not_shadow_messages(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_INTERNAL_URL", "http://127.0.0.1:8000")
    server = start_server.create_server(
        agent.AGENT_APP,
        agent.CONNECTION_MANAGER.get_default_connection_configuration(),
    )
    routes = [(route.method, route.resource.canonical) for route in server.router.routes()]
    assert ("POST", "/api/messages") in routes
    assert any(path == "/{tail}" for _method, path in routes)
    assert start_server._control_plane_path("/manager")
    assert start_server._control_plane_path("/api/ag-ui")
    assert start_server._control_plane_path("/docs")
    assert not start_server._control_plane_path("/api/messages")
    assert not start_server._control_plane_path("/arbitrary/proxy/target")


def test_agent_instance_rejects_a_different_manager():
    context = FakeContext()
    context.activity.from_property.aad_object_id = "principal_sam"
    with pytest.raises(PermissionError, match="not this agent instance's manager"):
        agent._manager_id(context)


@pytest.mark.asyncio
async def test_handlers_are_runnable_and_flush(monkeypatch):
    context = FakeContext()
    await agent.on_members_added(context, None)
    assert context.sent == [agent.config.SPEC.agent.introduction]

    flushed = []
    monkeypatch.setattr(agent.observability, "force_flush", lambda: flushed.append(True))
    await agent.on_message(context, None)
    assert "explicit offline mode" in context.sent[-1]
    assert flushed == [True]
    assert agent.agent_identity.current_context() is None
