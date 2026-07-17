"""Agent 365 Observability SDK wiring for turns and tools."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

from . import config


_token_cache: dict[tuple[str, str], str] = {}
_configured = False


def cache_export_token(agent_id: str, tenant_id: str, token: str | None) -> None:
    if token:
        _token_cache[(agent_id, tenant_id)] = token


def resolve_export_token(agent_id: str, tenant_id: str) -> str | None:
    """Return only an exact-pair bare token; never leak another agent's token."""
    return _token_cache.get((agent_id, tenant_id))


def configure_a365() -> bool:
    global _configured
    if _configured:
        return True
    if not config.ENABLE_A365_OBSERVABILITY:
        return False
    from microsoft_agents_a365.observability.core import configure

    _configured = bool(
        configure(
            service_name=config.SPEC.observability.service_name,
            service_namespace=config.SPEC.observability.service_namespace,
            token_resolver=resolve_export_token,
            cluster_category=config.SPEC.observability.cluster_category,
        )
    )
    return _configured


def _agent_details(conversation_id: str | None = None):
    from microsoft_agents_a365.observability.core import AgentDetails
    from .agent_identity import current_context

    context = current_context()
    return AgentDetails(
        agent_id=(context.agent_id if context and context.agent_id else config.AGENT_ID)
        or config.AGENT_DISPLAY_NAME,
        agent_name=config.AGENT_DISPLAY_NAME,
        agent_blueprint_id=config.AGENT_BLUEPRINT_ID or None,
        tenant_id=config.AGENT_TENANT_ID or None,
        conversation_id=conversation_id,
    )


@contextlib.contextmanager
def invoke_agent_scope(
    content: str, *, session_id: str, conversation_id: str
) -> Iterator[Any]:
    if not config.ENABLE_A365_OBSERVABILITY:
        yield None
        return
    from microsoft_agents_a365.observability.core import (
        ExecutionType,
        InvokeAgentDetails,
        InvokeAgentScope,
        Request,
        TenantDetails,
    )

    with InvokeAgentScope(
        invoke_agent_details=InvokeAgentDetails(
            details=_agent_details(conversation_id), session_id=session_id
        ),
        tenant_details=TenantDetails(tenant_id=config.AGENT_TENANT_ID or "unknown"),
        request=Request(
            content=content,
            execution_type=ExecutionType.HUMAN_TO_AGENT,
            session_id=session_id,
        ),
    ) as scope:
        yield scope


@contextlib.contextmanager
def execute_tool_scope(
    tool_name: str, arguments: dict[str, Any], conversation_id: str | None = None
) -> Iterator[Any]:
    if not config.ENABLE_A365_OBSERVABILITY:
        yield None
        return
    from microsoft_agents_a365.observability.core import (
        ExecuteToolScope,
        TenantDetails,
        ToolCallDetails,
        ToolType,
    )

    with ExecuteToolScope(
        details=ToolCallDetails(
            tool_name=tool_name,
            arguments=json.dumps(arguments, default=str),
            tool_type=getattr(ToolType.FUNCTION, "value", ToolType.FUNCTION),
        ),
        agent_details=_agent_details(conversation_id),
        tenant_details=TenantDetails(tenant_id=config.AGENT_TENANT_ID or "unknown"),
    ) as scope:
        yield scope


async def setup_export_token(auth: Any, context: Any) -> None:
    if not (
        config.ENABLE_A365_OBSERVABILITY
        and config.ENABLE_A365_OBSERVABILITY_EXPORTER
    ):
        return
    from microsoft_agents_a365.runtime.environment_utils import (
        get_observability_authentication_scope,
    )

    recipient = getattr(getattr(context, "activity", None), "recipient", None)
    agent_id = getattr(recipient, "agentic_app_id", None) or config.AGENT_ID
    tenant_id = getattr(recipient, "tenant_id", None) or config.AGENT_TENANT_ID
    if not agent_id or not tenant_id:
        raise RuntimeError("Agent and tenant IDs are required for A365 telemetry export")
    response = await auth.exchange_token(
        context,
        get_observability_authentication_scope(),
        config.AGENTIC_HANDLER_ID,
    )
    token = getattr(response, "token", None)
    if not token:
        raise RuntimeError("A365 observability token exchange returned no token")
    cache_export_token(agent_id, tenant_id, token)


async def setup_standalone_export_token() -> None:
    if not (
        config.ENABLE_A365_OBSERVABILITY
        and config.ENABLE_A365_OBSERVABILITY_EXPORTER
    ):
        return
    from microsoft_agents_a365.runtime.environment_utils import (
        get_observability_authentication_scope,
    )
    from .agent_identity import acquire_agentic_user_token

    from .agent_identity import current_context

    context = current_context()
    agent_id = context.agent_id if context and context.agent_id else config.AGENT_ID
    if not agent_id or not config.AGENT_TENANT_ID:
        raise RuntimeError("Agent and tenant IDs are required for A365 telemetry export")
    token = await acquire_agentic_user_token(
        get_observability_authentication_scope()
    )
    if not token:
        raise RuntimeError("Standalone agentic-user observability token mint failed")
    cache_export_token(agent_id, config.AGENT_TENANT_ID, token)


def record_response(scope: Any, response: Any) -> None:
    if scope is not None:
        scope.record_response(response if isinstance(response, str) else json.dumps(response, default=str))


def record_error(scope: Any, error: Exception) -> None:
    if scope is not None:
        scope.record_error(error)


def force_flush(timeout_millis: int = 10000) -> None:
    from opentelemetry import trace

    provider = trace.get_tracer_provider()
    flush = getattr(provider, "force_flush", None)
    if flush:
        flush(timeout_millis)
