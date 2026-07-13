"""
The agent's **own** identity token — minted with no incoming turn context.

Unlike On-Behalf-Of (which exchanges a *signed-in manager's* token and therefore
needs a live turn), the Agent 365 **agentic-user** token is minted from the
blueprint's own credentials plus two identifiers — the per-instance Entra **app
id** and the **agent-user object id**. Under the hood the Microsoft Agents SDK
(``MsalAuth.get_agentic_user_token``) runs a chain of OAuth2 **client-credentials**
calls (``acquire_token_for_client`` with ``fmi_path`` and ``grant_type=user_fic``)
— it never calls ``acquire_token_on_behalf_of`` and never needs a user assertion.

The practical consequence: **the agent can act as itself from a system hook** (a
sweep, a timer, a signal landing) with no human in a turn. That is the autonomous
half of the identity model:

* **OBO** → only when acting *as the manager* (reading/writing the manager's own
  Microsoft 365 data — Work IQ grounding, sending from the CSM's mailbox).
* **This (agentic-user) token** → everything else (Gainsight, Snowflake, the
  agent's own actions), governed as the agent's first-class Entra Agent ID.

Everything here is **best-effort and never raises**: when the federation isn't
configured/enabled, or the instance identifiers can't be resolved, the helpers
return ``(None, False)`` and callers fall back (managed identity / simulated),
so the agent keeps working exactly as before.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from . import config

logger = logging.getLogger(__name__)

# Cache minted tokens by (agent_user_oid, scope-key) with a soft expiry.
_LOCK = threading.RLock()
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_TTL_SECONDS = 50 * 60  # tokens last ~60-90 min; refresh comfortably early.

# Lazily-built blueprint connection (the confidential client that mints).
_connection: object | None = None
_connection_tried = False
_last_error: str | None = None


def _get_connection():
    """Build (once) the blueprint connection used to mint agentic-user tokens.

    Uses the same ``CONNECTIONS__SERVICE_CONNECTION__*`` configuration the agent
    host already consumes, so no new credentials are introduced. Returns ``None``
    if the SDK/connection cannot be constructed (e.g. minimal hosts).
    """
    global _connection, _connection_tried, _last_error
    if _connection is not None or _connection_tried:
        return _connection
    _connection_tried = True
    try:
        from microsoft_agents.authentication.msal import MsalConnectionManager
        from microsoft_agents.activity import load_configuration_from_env

        manager = MsalConnectionManager(**load_configuration_from_env(os.environ))
        _connection = manager.get_default_connection()
    except Exception as exc:  # pragma: no cover - depends on host/SDK
        _last_error = f"connection unavailable: {exc}"
        logger.info("Agentic identity: %s", _last_error)
        _connection = None
    return _connection


def resolve_instance(manager_id: str | None = None) -> tuple[str, str]:
    """Resolve ``(instance_app_id, agent_user_oid)`` for the given manager.

    Configuration overrides win (pin a single instance); otherwise the values are
    taken from live Agent 365 instance discovery. Returns empty strings when not
    resolvable (the caller then falls back).
    """
    instance_app_id = config.AGENT_INSTANCE_APP_ID
    agent_user_oid = config.AGENTIC_USER_ID
    if (not agent_user_oid or not instance_app_id) and manager_id:
        try:
            from . import agent_instances

            cached = agent_instances.cached_csm_instances() or {}
            rec = cached.get(manager_id) or {}
            agent_user_oid = agent_user_oid or rec.get("agentUserId") or ""
            instance_app_id = instance_app_id or rec.get("instanceAppId") or ""
        except Exception:  # pragma: no cover - discovery optional
            pass
    return instance_app_id, agent_user_oid


async def acquire_agent_token(
    scopes: list[str], *, manager_id: str | None = None
) -> tuple[str | None, bool]:
    """Mint the agent's own agentic-user token for ``scopes`` (no turn needed).

    Returns ``(token, is_real)``. ``is_real`` is ``True`` only when a real token
    was minted. Never raises; returns ``(None, False)`` on any failure so callers
    fall back cleanly.
    """
    global _last_error
    if not config.ENABLE_AGENTIC_IDENTITY:
        return None, False
    tenant_id = config.AGENT_TENANT_ID
    instance_app_id, agent_user_oid = resolve_instance(manager_id)
    if not (tenant_id and instance_app_id and agent_user_oid):
        _last_error = "instance identifiers not resolved (tenant/instance app id/agent-user oid)"
        return None, False

    cache_key = (agent_user_oid, " ".join(sorted(scopes)))
    with _LOCK:
        hit = _token_cache.get(cache_key)
        if hit and hit[1] > time.time():
            return hit[0], True

    conn = _get_connection()
    if conn is None:
        return None, False
    try:
        token = await conn.get_agentic_user_token(
            tenant_id, instance_app_id, agent_user_oid, scopes
        )
    except Exception as exc:  # pragma: no cover - depends on live federation
        _last_error = f"mint failed: {exc}"
        logger.info("Agentic identity mint failed: %s", exc)
        return None, False
    if not token:
        _last_error = "mint returned no token (federation not enabled for this instance?)"
        return None, False

    with _LOCK:
        _token_cache[cache_key] = (token, time.time() + _TOKEN_TTL_SECONDS)
    _last_error = None
    logger.info("Agentic identity: minted agent-user token (instance=%s).", instance_app_id)
    return token, True


async def acting_identity(manager_id: str | None = None) -> dict:
    """A truthful description of *who* the agent acts as for autonomous work.

    Attempts a mint (cached) and reports whether the agent has its own governed
    Entra Agent ID token available, or is falling back to the host managed
    identity / simulation. Used for honest narration + the technical dashboard.
    """
    instance_app_id, agent_user_oid = resolve_instance(manager_id)
    token, ok = await acquire_agent_token(
        [config.AGENTIC_USER_GRAPH_SCOPE], manager_id=manager_id
    )
    return {
        "minted": ok,
        "enabled": config.ENABLE_AGENTIC_IDENTITY,
        "agenticUserId": agent_user_oid or None,
        "instanceAppId": instance_app_id or None,
        "label": (
            "Entra Agent ID (agentic-user token)"
            if ok
            else "host managed identity / simulated (agent-user token unavailable)"
        ),
        "reason": None if ok else _last_error,
    }
