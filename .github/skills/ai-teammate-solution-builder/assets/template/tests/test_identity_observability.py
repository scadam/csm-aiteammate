from __future__ import annotations

from types import SimpleNamespace
import contextlib
from datetime import datetime, timedelta, timezone

import pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from app import agent_identity, observability
from app import config
from app.identity import PlatformIdentityProvider


class FakeAuthorization:
    def __init__(self):
        self.calls = []

    async def exchange_token(self, context, scopes, auth_handler_id):
        self.calls.append((context, scopes, auth_handler_id))
        return SimpleNamespace(token="manager-obo-token")


class FakeConnection:
    def __init__(self):
        self.calls = []

    async def get_agentic_user_token(self, tenant_id, instance_id, user_id, scopes):
        self.calls.append((tenant_id, instance_id, user_id, scopes))
        return "agentic-user-token"


@pytest.mark.asyncio
async def test_manager_obo_and_agentic_user_are_distinct(monkeypatch):
    authorization = FakeAuthorization()
    connection = FakeConnection()
    agent_identity.configure_identity(authorization, connection)
    monkeypatch.setattr(agent_identity.config, "AGENT_TENANT_ID", "tenant")
    monkeypatch.setattr(agent_identity.config, "AGENT_INSTANCE_APP_ID", "instance")
    monkeypatch.setattr(agent_identity.config, "AGENTIC_USER_ID", "agent-user")
    turn_context = object()
    token = agent_identity.set_context(
        agent_identity.AgentRequestContext("owner_alex", "conversation", turn_context)
    )
    try:
        assert await agent_identity.exchange_manager_obo(["scope.manager"]) == "manager-obo-token"
        assert await agent_identity.acquire_agentic_user_token(["scope.agent"]) == "agentic-user-token"
    finally:
        agent_identity.reset_context(token)
    assert authorization.calls == [(turn_context, ["scope.manager"], "OBO")]
    assert connection.calls == [("tenant", "instance", "agent-user", ["scope.agent"])]


@pytest.mark.asyncio
async def test_manager_obo_fails_closed_without_turn_context():
    token = agent_identity.set_context(
        agent_identity.AgentRequestContext("owner_alex", "autonomous", None)
    )
    try:
        assert await agent_identity.exchange_manager_obo(["scope.manager"]) is None
        with pytest.raises(PermissionError, match="manager_obo"):
            await agent_identity.require_token("manager_obo", ["scope.manager"])
    finally:
        agent_identity.reset_context(token)


@pytest.mark.asyncio
async def test_validated_inbound_assertion_uses_named_obo_connection(monkeypatch):
    calls = []

    class OboConnection:
        async def acquire_token_on_behalf_of(self, scopes, user_assertion):
            calls.append((scopes, user_assertion))
            return "downstream-obo-token"

    monkeypatch.setattr(
        agent_identity, "_standalone_obo_connection", lambda: OboConnection()
    )
    token = agent_identity.set_context(
        agent_identity.AgentRequestContext(
            "owner_alex", "mcp", inbound_assertion="validated-gateway-token"
        )
    )
    try:
        assert await agent_identity.exchange_manager_obo(["scope.manager"]) == "downstream-obo-token"
    finally:
        agent_identity.reset_context(token)
    assert calls == [(["scope.manager"], "validated-gateway-token")]


@pytest.mark.asyncio
async def test_agentic_user_lazily_uses_standalone_service_connection(monkeypatch):
    connection = FakeConnection()
    monkeypatch.setattr(agent_identity, "_connection", None)
    monkeypatch.setattr(agent_identity, "_standalone_connection", lambda: connection)
    monkeypatch.setattr(agent_identity.config, "AGENT_TENANT_ID", "tenant")
    monkeypatch.setattr(agent_identity.config, "AGENT_INSTANCE_APP_ID", "instance")
    monkeypatch.setattr(agent_identity.config, "AGENTIC_USER_ID", "agent-user")
    assert await agent_identity.acquire_agentic_user_token(["scope.agent"]) == "agentic-user-token"
    assert connection.calls == [("tenant", "instance", "agent-user", ["scope.agent"])]


def test_observability_token_resolver_is_exact_pair():
    observability._token_cache.clear()
    observability.cache_export_token("agent-a", "tenant-a", "bare-token")
    assert observability.resolve_export_token("agent-a", "tenant-a") == "bare-token"
    assert observability.resolve_export_token("agent-b", "tenant-a") is None
    assert observability.resolve_export_token("agent-a", "tenant-b") is None


def test_a365_scopes_record_response_and_error(monkeypatch):
    recorded = []

    class FakeScope:
        def record_response(self, value):
            recorded.append(("response", value))

        def record_error(self, error):
            recorded.append(("error", str(error)))

    monkeypatch.setattr(observability.config, "ENABLE_A365_OBSERVABILITY", True)
    fake_scope = FakeScope()
    monkeypatch.setattr(
        "microsoft_agents_a365.observability.core.InvokeAgentScope",
        lambda **kwargs: contextlib.nullcontext(fake_scope),
    )
    with observability.invoke_agent_scope(
        "hello", session_id="owner:conversation", conversation_id="conversation"
    ) as scope:
        observability.record_response(scope, "answer")
        observability.record_error(scope, ValueError("bad"))
    assert recorded == [("response", "answer"), ("error", "bad")]


@pytest.mark.asyncio
async def test_observability_export_token_uses_agentic_handler(monkeypatch):
    class Auth:
        def __init__(self):
            self.calls = []

        async def exchange_token(self, context, scopes, auth_handler_id):
            self.calls.append((context, scopes, auth_handler_id))
            return SimpleNamespace(token="bare-observability-token")

    auth = Auth()
    context = SimpleNamespace(
        activity=SimpleNamespace(
            recipient=SimpleNamespace(agentic_app_id="agent", tenant_id="tenant")
        )
    )
    monkeypatch.setattr(observability.config, "ENABLE_A365_OBSERVABILITY", True)
    monkeypatch.setattr(observability.config, "ENABLE_A365_OBSERVABILITY_EXPORTER", True)
    observability._token_cache.clear()
    await observability.setup_export_token(auth, context)
    assert auth.calls[0][0] is context
    assert auth.calls[0][2] == "AGENTIC"
    assert observability.resolve_export_token("agent", "tenant") == "bare-observability-token"


@pytest.mark.asyncio
async def test_production_teams_token_requires_exact_ets_contract(monkeypatch):
    tenant = "11111111-1111-4111-8111-111111111111"
    blueprint = "22222222-2222-4222-8222-222222222222"
    teams_web = "5e3ce6c0-2b1f-4285-8d4b-75ee78787346"
    audience = f"api://control.example.com/{blueprint}"
    manager = config.SPEC.managers[0]
    monkeypatch.setattr(config, "DEVELOPMENT_MODE", False)
    monkeypatch.setattr(config, "AGENT_TENANT_ID", tenant)
    monkeypatch.setattr(config, "AGENT_BLUEPRINT_ID", blueprint)
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN_ISSUER", f"https://login.microsoftonline.com/{tenant}/v2.0")
    monkeypatch.setattr(config, "CONTROL_PLANE_TOKEN_AUDIENCE", audience)
    monkeypatch.setattr(config, "CONTROL_PLANE_JWKS_URL", "https://login.example.test/keys")
    monkeypatch.setattr(config, "CONTROL_PLANE_REQUIRED_SCOPE", "access_agent_as_user")
    monkeypatch.setattr(config, "CONTROL_PLANE_ALLOWED_CLIENT_IDS", {teams_web})

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class SigningKey:
        key = private_key.public_key()

    class Jwks:
        def get_signing_key_from_jwt(self, _token):
            return SigningKey()

    provider = PlatformIdentityProvider(config.SPEC)
    provider._jwks = Jwks()
    now = datetime.now(timezone.utc)

    def token(**overrides):
        claims = {
            "exp": now + timedelta(minutes=5),
            "iat": now,
            "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
            "aud": audience,
            "oid": manager.principal_id,
            "tid": tenant,
            "scp": "access_agent_as_user",
            "azp": teams_web,
        }
        claims.update(overrides)
        return jwt.encode(claims, private_key, algorithm="RS256")

    def request(value):
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/me",
                "headers": [(b"authorization", f"Bearer {value}".encode())],
            }
        )

    principal = await provider.resolve(request(token()))
    assert principal.manager_id == manager.id
    bare = await provider.resolve(request(token(aud=blueprint)))
    assert bare.manager_id == manager.id
    for invalid in (
        token(aud="api://wrong/app"),
        token(tid="wrong-tenant"),
        token(iss="https://login.microsoftonline.com/wrong/v2.0"),
        token(scp="User.Read"),
        token(azp="untrusted-client"),
    ):
        with pytest.raises(Exception):
            await provider.resolve(request(invalid))
