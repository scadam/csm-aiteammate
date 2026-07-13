"""Tests for the agent's own-identity token (agentic-user) and the layered
OBO-vs-agent-token strategy in :mod:`src.identity`."""

import asyncio

from src import agentic_identity, config, identity


def test_acquire_agent_token_disabled_returns_false(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_AGENTIC_IDENTITY", False)
    token, ok = asyncio.run(agentic_identity.acquire_agent_token(["scope/.default"]))
    assert token is None and ok is False


def test_acquire_agent_token_needs_identifiers(monkeypatch):
    # Enabled, but no instance app id / agent-user oid resolvable → graceful (None, False).
    monkeypatch.setattr(config, "ENABLE_AGENTIC_IDENTITY", True)
    monkeypatch.setattr(config, "AGENT_INSTANCE_APP_ID", "")
    monkeypatch.setattr(config, "AGENTIC_USER_ID", "")
    token, ok = asyncio.run(
        agentic_identity.acquire_agent_token(["scope/.default"], manager_id="csm-nobody")
    )
    assert token is None and ok is False


def test_acquire_agent_token_mints_when_configured(monkeypatch):
    # With identifiers + a stubbed connection, the token is minted with NO turn context.
    monkeypatch.setattr(config, "ENABLE_AGENTIC_IDENTITY", True)
    monkeypatch.setattr(config, "AGENT_TENANT_ID", "tenant-1")
    monkeypatch.setattr(config, "AGENT_INSTANCE_APP_ID", "instance-app-1")
    monkeypatch.setattr(config, "AGENTIC_USER_ID", "agent-user-oid-1")
    agentic_identity._token_cache.clear()

    seen = {}

    class _StubConn:
        async def get_agentic_user_token(self, tenant, instance, user, scopes):
            seen.update(tenant=tenant, instance=instance, user=user, scopes=tuple(scopes))
            return "minted-agent-token"

    monkeypatch.setattr(agentic_identity, "_get_connection", lambda: _StubConn())

    token, ok = asyncio.run(agentic_identity.acquire_agent_token(["api://x/.default"]))
    assert ok is True and token == "minted-agent-token"
    # It used the identifiers, not any incoming user assertion.
    assert seen == {
        "tenant": "tenant-1",
        "instance": "instance-app-1",
        "user": "agent-user-oid-1",
        "scopes": ("api://x/.default",),
    }


def test_delegated_token_prefers_agent_for_non_manager(monkeypatch):
    # "Everything else" (as_manager=False) should use the agent's own token.
    async def _fake_agent_token(scopes, *, manager_id=None):
        return "agent-tok", True

    monkeypatch.setattr(identity, "acquire_agent_token", _fake_agent_token)
    token, real = asyncio.run(identity.acquire_delegated_token("gainsight", ["s"]))
    assert token == "agent-tok" and real is True


def test_delegated_token_uses_obo_when_as_manager(monkeypatch):
    # "As the manager" should use OBO, never the agent token.
    async def _fake_obo(scopes):
        return "obo-tok"

    async def _boom_agent(scopes, *, manager_id=None):  # must NOT be called
        raise AssertionError("agent token must not be used for as_manager=True")

    monkeypatch.setattr(identity, "exchange_obo_token", _fake_obo)
    monkeypatch.setattr(identity, "acquire_agent_token", _boom_agent)
    token, real = asyncio.run(
        identity.acquire_delegated_token("workiq", ["s"], as_manager=True)
    )
    assert token == "obo-tok" and real is True


def test_delegated_token_falls_back_to_sim(monkeypatch):
    # No agent token, no OBO → clearly-marked simulated delegated token.
    async def _no_agent(scopes, *, manager_id=None):
        return None, False

    async def _no_obo(scopes):
        return None

    monkeypatch.setattr(identity, "acquire_agent_token", _no_agent)
    monkeypatch.setattr(identity, "exchange_obo_token", _no_obo)
    token, real = asyncio.run(identity.acquire_delegated_token("snowflake", ["s"]))
    assert real is False and token.startswith("sim-deleg:snowflake:")
