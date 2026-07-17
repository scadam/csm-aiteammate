"""Per-turn manager OBO and autonomous agentic-user identity."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from os import environ
from typing import Any, Protocol

from . import config


@dataclass(frozen=True)
class AgentRequestContext:
    manager_id: str
    conversation_id: str
    turn_context: Any | None = None
    principal_id: str = ""
    activity_id: str = ""
    inbound_assertion: str = ""
    agent_id: str = ""
    instance_app_id: str = ""
    agentic_user_id: str = ""

    @property
    def session_key(self) -> str:
        return f"{self.manager_id}:{self.conversation_id}"


class AgentAuthorization(Protocol):
    async def exchange_token(self, context: Any, scopes: list[str], auth_handler_id: str) -> Any: ...


_current: contextvars.ContextVar[AgentRequestContext | None] = contextvars.ContextVar(
    "agent_request_context", default=None
)
_authorization: AgentAuthorization | None = None
_connection: Any | None = None
_obo_connection: Any | None = None


def configure_identity(authorization: AgentAuthorization, connection: Any) -> None:
    global _authorization, _connection
    _authorization = authorization
    _connection = connection


def set_context(context: AgentRequestContext) -> contextvars.Token:
    return _current.set(context)


def reset_context(token: contextvars.Token) -> None:
    _current.reset(token)


def current_context() -> AgentRequestContext | None:
    return _current.get()


async def exchange_manager_obo(scopes: list[str]) -> str | None:
    """Exchange the signed-in manager token; never substitute another identity."""
    context = _current.get()
    if context is None:
        return None
    if context.turn_context is not None and _authorization is not None:
        response = await _authorization.exchange_token(
            context.turn_context, scopes, config.OBO_HANDLER_ID
        )
        return getattr(response, "token", None)
    if context.inbound_assertion:
        return await _exchange_assertion_obo(context.inbound_assertion, scopes)
    return None


async def acquire_agentic_user_token(scopes: list[str]) -> str | None:
    """Mint the teammate's own Agent ID token without an incoming manager turn."""
    connection = _connection or _standalone_connection()
    if connection is None:
        return None
    context = _current.get()
    instance_app_id = (
        context.instance_app_id if context and context.instance_app_id else config.AGENT_INSTANCE_APP_ID
    )
    agentic_user_id = (
        context.agentic_user_id if context and context.agentic_user_id else config.AGENTIC_USER_ID
    )
    if not all([config.AGENT_TENANT_ID, instance_app_id, agentic_user_id]):
        return None
    return await connection.get_agentic_user_token(
        config.AGENT_TENANT_ID,
        instance_app_id,
        agentic_user_id,
        scopes,
    )


def manager_agent_identity(manager_id: str) -> dict[str, str]:
    if manager_id != config.AGENT_MANAGER_ID:
        raise PermissionError("This Agent ID is assigned to another manager")
    return {
        "agent_id": config.AGENT_ID,
        "instance_app_id": config.AGENT_INSTANCE_APP_ID,
        "agentic_user_id": config.AGENTIC_USER_ID,
    }


def request_context(
    manager_id: str,
    conversation_id: str,
    *,
    turn_context: Any | None = None,
    principal_id: str = "",
    activity_id: str = "",
    inbound_assertion: str = "",
) -> AgentRequestContext:
    return AgentRequestContext(
        manager_id=manager_id,
        conversation_id=conversation_id,
        turn_context=turn_context,
        principal_id=principal_id,
        activity_id=activity_id,
        inbound_assertion=inbound_assertion,
        **manager_agent_identity(manager_id),
    )


async def acquire_token(mode: str, scopes: list[str], token_env: str = "") -> str | None:
    if mode == "manager_obo":
        return await exchange_manager_obo(scopes)
    if mode == "agentic_user":
        return await acquire_agentic_user_token(scopes)
    if mode == "managed_identity":
        from azure.identity.aio import DefaultAzureCredential

        credential = DefaultAzureCredential()
        try:
            token = await credential.get_token(*(scopes or [config.AZURE_OPENAI_SCOPE]))
            return token.token
        finally:
            await credential.close()
    if mode in {"bearer_env", "native"} and token_env:
        import os

        return os.getenv(token_env) or None
    if mode == "none":
        return None
    raise RuntimeError(f"No token adapter is configured for identity mode {mode!r}")


async def require_token(mode: str, scopes: list[str], token_env: str = "") -> str:
    token = await acquire_token(mode, scopes, token_env)
    if not token:
        raise PermissionError(f"A real {mode} token is required for this operation")
    return token


def _standalone_connection() -> Any | None:
    global _connection
    if _connection is not None:
        return _connection
    try:
        from microsoft_agents.activity import load_configuration_from_env
        from microsoft_agents.authentication.msal import MsalConnectionManager

        manager = MsalConnectionManager(**load_configuration_from_env(environ))
        _connection = manager.get_default_connection()
    except (ImportError, ValueError):
        return None
    return _connection


async def _exchange_assertion_obo(assertion: str, scopes: list[str]) -> str | None:
    connection = _standalone_obo_connection()
    if connection is None:
        return None
    return await connection.acquire_token_on_behalf_of(scopes, assertion)


def _standalone_obo_connection() -> Any | None:
    global _obo_connection
    if _obo_connection is not None:
        return _obo_connection
    try:
        from microsoft_agents.activity import load_configuration_from_env
        from microsoft_agents.authentication.msal import MsalConnectionManager

        manager = MsalConnectionManager(**load_configuration_from_env(environ))
        _obo_connection = manager.get_connection("OBO")
    except (ImportError, ValueError):
        return None
    return _obo_connection
