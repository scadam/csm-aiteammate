"""
Agent identity and acting-on-behalf-of (OBO) helpers.

The AI Teammate has its **own** Entra Agent ID (used to authenticate the agent,
register on the A365 Tooling Gateway, and emit telemetry) but always acts **on
behalf of its manager** via OBO token exchange. Any manager-scoped action must
obtain an OBO token first — never use the bare agent app token for a
manager-scoped action.

This module keeps a per-turn :class:`RequestContext` (manager id + conversation
id + the current ``TurnContext``) so tools can resolve the manager and request
OBO tokens without threading the context through every call. State is keyed by
``manager_id:conversation_id`` so one manager's context never leaks to another.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass

from . import config, data_store

logger = logging.getLogger(__name__)


@dataclass
class RequestContext:
    manager_id: str
    conversation_id: str
    turn_context: object | None = None  # microsoft_agents TurnContext (kept untyped here)
    entra_object_id: str | None = None  # the signed-in user's Entra oid (when known)
    upn: str | None = None              # the signed-in user's UPN (when known)

    @property
    def session_key(self) -> str:
        return f"{self.manager_id}:{self.conversation_id}"


@dataclass
class UserPrincipal:
    """The resolved, signed-in human acting through the agent / dashboards.

    Identity is the **join key**: the Entra ``object_id`` (or UPN) is matched to a
    CSM (manager) and/or the programme owner (sponsor) in the fixtures. This is
    what decides which dashboard a person sees and which accounts they own.
    """

    entra_object_id: str | None
    upn: str | None
    display_name: str
    manager_id: str | None          # the CSM record this user is, if any
    is_owner: bool                  # true if this user is the programme owner/sponsor
    owner_id: str | None = None
    source: str = "unknown"         # teams_sso | session | default

    @property
    def roles(self) -> list[str]:
        roles = []
        if self.manager_id:
            roles.append("manager")
        if self.is_owner:
            roles.append("owner")
        return roles

    def to_public(self) -> dict:
        return {
            "id": self.entra_object_id,
            "upn": self.upn,
            "name": self.display_name,
            "managerId": self.manager_id,
            "isOwner": self.is_owner,
            "ownerId": self.owner_id,
            "roles": self.roles,
            "source": self.source,
        }


_current: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "csm_request_context", default=None
)


def set_request_context(ctx: RequestContext) -> contextvars.Token:
    return _current.set(ctx)


def reset_request_context(token: contextvars.Token) -> None:
    _current.reset(token)


def current_context() -> RequestContext | None:
    return _current.get()


def current_manager_id() -> str:
    ctx = _current.get()
    return ctx.manager_id if ctx else config.AGENT_MANAGER_USER_ID


def current_conversation_id() -> str:
    ctx = _current.get()
    return ctx.conversation_id if ctx else "default"


def resolve_manager(manager_id: str | None = None) -> dict | None:
    """Return the manager (CSM) record for ``manager_id`` (defaults to current)."""
    mid = manager_id or current_manager_id()
    return data_store.get("managers", "manager_id", mid)


def manager_owns_account(account_id: str, manager_id: str | None = None) -> bool:
    """True if the given account is assigned to the manager the agent acts for."""
    account = data_store.get("accounts", "account_id", account_id)
    if account is None:
        return False
    return account.get("csm_manager_id") == (manager_id or current_manager_id())


async def exchange_obo_token(scopes: list[str]) -> str | None:
    """
    Acquire an OBO token for the current manager, scoped to a downstream resource.

    Uses the Microsoft Agents SDK ``Authorization.exchange_token`` (verified
    three-argument shape: ``exchange_token(context, scopes, auth_handler_id)``).
    Returns the bare token string, or ``None`` if no turn context is available
    (e.g. background processing) or the exchange fails.
    """
    ctx = _current.get()
    if ctx is None or ctx.turn_context is None:
        logger.debug("No turn context; cannot perform OBO exchange.")
        return None
    try:
        from .agent import AGENT_APP  # imported lazily to avoid a cycle

        token_response = await AGENT_APP.auth.exchange_token(
            ctx.turn_context, scopes, config.OBO_HANDLER_ID
        )
        return getattr(token_response, "token", None)
    except Exception as exc:  # pragma: no cover - depends on live auth
        logger.warning("OBO token exchange failed: %s", exc)
        return None


async def get_user_token(handler_id: str | None = None) -> str | None:
    """Retrieve the manager's token for a handler (e.g. GITHUB) for per-user identity."""
    ctx = _current.get()
    if ctx is None or ctx.turn_context is None:
        return None
    try:
        from .agent import AGENT_APP

        token_response = await AGENT_APP.auth.get_token(
            ctx.turn_context, handler_id or config.GITHUB_AUTH_HANDLER_ID
        )
        return getattr(token_response, "token", None) if token_response else None
    except Exception as exc:  # pragma: no cover
        logger.debug("get_user_token failed: %s", exc)
        return None


# ── signed-in user resolution (Teams SSO / session → CSM / owner) ───────
def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def resolve_user(
    *, object_id: str | None = None, upn: str | None = None,
    display_name: str | None = None, source: str = "unknown",
) -> UserPrincipal:
    """Resolve a signed-in user (by Entra oid or UPN) to a CSM/owner principal.

    The oid/UPN is matched against the ``managers`` and ``owners`` fixtures. In
    production these records hold the real Entra object ids; here they hold the
    real tenant users' oids/UPNs, so the mapping is exact.
    """
    oid = _norm(object_id)
    upn_n = _norm(upn)

    manager = None
    for m in data_store.table("managers"):
        if (oid and _norm(m.get("entra_object_id")) == oid) or (upn_n and _norm(m.get("upn")) == upn_n):
            manager = m
            break

    owner = None
    for o in data_store.table("owners"):
        if (oid and _norm(o.get("entra_object_id")) == oid) or (upn_n and _norm(o.get("upn")) == upn_n):
            owner = o
            break

    name = display_name or (manager or {}).get("display_name") or (owner or {}).get("display_name") or upn or "User"
    return UserPrincipal(
        entra_object_id=object_id,
        upn=upn or (manager or {}).get("upn") or (owner or {}).get("upn"),
        display_name=name,
        manager_id=(manager or {}).get("manager_id"),
        is_owner=owner is not None,
        owner_id=(owner or {}).get("owner_id") or (manager or {}).get("owner_id"),
        source=source,
    )


def resolve_user_by_manager_id(manager_id: str, source: str = "session") -> UserPrincipal:
    """Resolve a principal from a CSM manager id (the simulated-picker path)."""
    m = data_store.get("managers", "manager_id", manager_id) or {}
    return resolve_user(object_id=m.get("entra_object_id"), upn=m.get("upn"),
                        display_name=m.get("display_name"), source=source)


async def acquire_agent_token(
    scopes: list[str], *, manager_id: str | None = None
) -> tuple[str | None, bool]:
    """Mint the agent's **own** agentic-user token (acts as the agent, no turn).

    Thin pass-through to :mod:`src.agentic_identity`. Returns ``(token, is_real)``
    and never raises. This is the token used for everything the agent does **not**
    do as the manager.
    """
    from . import agentic_identity

    return await agentic_identity.acquire_agent_token(scopes, manager_id=manager_id)


async def agentic_acting_identity(manager_id: str | None = None) -> dict:
    """A truthful snapshot of the identity the agent acts as for autonomous work."""
    from . import agentic_identity

    return await agentic_identity.acting_identity(manager_id=manager_id)


async def acquire_delegated_token(
    resource: str, scopes: list[str], *, as_manager: bool = False
) -> tuple[str | None, bool]:
    """
    Acquire a token for a downstream resource, picking the right identity:

    * ``as_manager=True`` → act **as the manager** (the manager's own data, e.g.
      Microsoft 365). Uses **On-Behalf-Of** (needs a live manager turn); falls
      back to a clearly-marked simulated delegated token offline.
    * otherwise (default) → act **as the agent** (Gainsight, Snowflake, the
      agent's own actions). Uses the agent's **own agentic-user token** — minted
      with no turn context, so this works for autonomous / system-triggered runs
      too. Falls back to a manager OBO token if one happens to be available (bot
      path), then to the simulated token.

    Returns ``(token, is_real)``. ``is_real`` is ``True`` for a real OBO or
    agentic-user token, ``False`` for the ``sim-deleg:…`` fallback (which lets the
    downstream simulators enforce per-identity RBAC exactly as the real APIs would).
    """
    if as_manager:
        real = await exchange_obo_token(scopes)
        if real:
            return real, True
    else:
        agent_token, ok = await acquire_agent_token(scopes)
        if ok and agent_token:
            return agent_token, True
        # Bot path may still carry a manager OBO token — prefer a real token over a sim.
        real = await exchange_obo_token(scopes)
        if real:
            return real, True
    ctx = _current.get()
    who = (ctx.upn if ctx and ctx.upn else None) or current_manager_id()
    return f"sim-deleg:{resource}:{who}", False


