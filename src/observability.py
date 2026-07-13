"""
A365 Observability SDK integration.

Uses the installed ``microsoft_agents_a365.observability.core`` package to emit
Agent 365 telemetry. Per the verified v1.x API, ``configure()`` takes the
``token_resolver`` directly (there is no ``exporter_options`` /
``Agent365ExporterOptions`` argument in this version). Both
``ENABLE_A365_OBSERVABILITY`` and ``ENABLE_A365_OBSERVABILITY_EXPORTER`` must be
``true`` for real export.

Each agent turn is wrapped in an ``InvokeAgentScope`` and each tool/MCP call in
an ``ExecuteToolScope`` (see :func:`invoke_agent_scope` / :func:`execute_tool_scope`).
When observability is disabled, the helpers return a no-op context manager so the
agent and tools run unchanged.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, Callable

from . import config

logger = logging.getLogger(__name__)

# A token resolver: (agent_id, tenant_id) -> bare access token (no "Bearer " prefix).
# The identity layer sets this; default reads from the per-turn token cache that
# the agent populates via the agentic-user exchange (see cache_observability_token).
_token_cache: dict[tuple[str, str], str] = {}


def cache_observability_token(tenant_id: str, agent_id: str, token: str | None) -> None:
    """Cache an agentic-user token for the A365 observability exporter.

    The exporter calls the resolver with ``(agent_id, tenant_id)`` whenever it
    needs to authenticate an export; we serve the most recently exchanged token
    for that pair. Mirrors the Microsoft sample's ``cache_agentic_token``.
    """
    if token:
        _token_cache[(agent_id, tenant_id)] = token


def _default_resolver(agent_id: str, tenant_id: str) -> str | None:
    return _token_cache.get((agent_id, tenant_id)) or next(iter(_token_cache.values()), None)


def a365_export_endpoint() -> str | None:
    """The real Agent 365 OTLP export URL for this agent, or ``None`` when not exporting.

    The A365 SDK builds this from the service host + tenant + agent id (the
    service-to-service ``/observabilityService/`` route). Surfaced read-only in the
    technical view so it shows the true endpoint rather than a blank.
    """
    if not (config.ENABLE_A365_OBSERVABILITY and config.ENABLE_A365_OBSERVABILITY_EXPORTER):
        return None
    tenant = config.AGENT_TENANT_ID
    agent = config.AGENT_ID
    if not (tenant and agent):
        return None
    host = config.A365_OBSERVABILITY_HOST.rstrip("/")
    return f"{host}/observabilityService/tenants/{tenant}/otlp/agents/{agent}/traces?api-version=1"


def observability_status() -> dict:
    """A truthful snapshot of the A365 observability configuration for the UI."""
    return {
        "serviceName": config.SERVICE_NAME,
        "serviceNamespace": config.SERVICE_NAMESPACE,
        "a365Enabled": config.ENABLE_A365_OBSERVABILITY,
        "a365ExporterEnabled": config.ENABLE_A365_OBSERVABILITY_EXPORTER,
        "clusterCategory": config.A365_CLUSTER_CATEGORY,
        "otelEndpoint": a365_export_endpoint() or (config.OTEL_EXPORTER_OTLP_ENDPOINT or None),
        "agentId": config.AGENT_ID or None,
        "tenantId": config.AGENT_TENANT_ID or None,
        "configured": _configured,
    }


_token_resolver: Callable[[str, str], str | None] = _default_resolver

_configured = False


def set_token_resolver(resolver: Callable[[str, str], str | None]) -> None:
    global _token_resolver
    _token_resolver = resolver


def configure_a365_observability() -> None:
    """Configure the A365 Observability SDK (no-op unless enabled)."""
    global _configured
    if _configured or not config.ENABLE_A365_OBSERVABILITY:
        if not config.ENABLE_A365_OBSERVABILITY:
            logger.info("A365 observability disabled (ENABLE_A365_OBSERVABILITY not true).")
        return
    try:
        from microsoft_agents_a365.observability.core import configure

        configure(
            service_name=config.SERVICE_NAME,
            service_namespace=config.SERVICE_NAMESPACE,
            token_resolver=lambda agent_id, tenant_id: _token_resolver(agent_id, tenant_id),
            cluster_category=config.A365_CLUSTER_CATEGORY,
        )
        _configured = True
        logger.info("A365 observability configured (exporter=%s).", config.ENABLE_A365_OBSERVABILITY_EXPORTER)
    except Exception as exc:  # pragma: no cover - SDK/preview variance
        logger.warning("A365 observability configure() failed: %s", exc)


def _agent_details(conversation_id: str | None):
    from microsoft_agents_a365.observability.core import AgentDetails

    return AgentDetails(
        agent_id=config.AGENT_ID or config.AGENT_DISPLAY_NAME,
        agent_name=config.AGENT_DISPLAY_NAME,
        agent_blueprint_id=config.AGENT_BLUEPRINT_ID or None,
        tenant_id=config.AGENT_TENANT_ID or None,
        conversation_id=conversation_id,
    )


def invoke_agent_scope(
    content: str,
    session_id: str | None = None,
    conversation_id: str | None = None,
):
    """Context manager wrapping a single agent turn (InvokeAgentScope)."""
    if not config.ENABLE_A365_OBSERVABILITY:
        return contextlib.nullcontext()
    try:
        from microsoft_agents_a365.observability.core import (
            InvokeAgentScope,
            InvokeAgentDetails,
            TenantDetails,
            Request,
            ExecutionType,
        )

        agent_details = _agent_details(conversation_id)
        return InvokeAgentScope(
            invoke_agent_details=InvokeAgentDetails(details=agent_details, session_id=session_id),
            tenant_details=TenantDetails(tenant_id=config.AGENT_TENANT_ID or "unknown"),
            request=Request(
                content=content,
                execution_type=ExecutionType.HUMAN_TO_AGENT,
                session_id=session_id,
            ),
        )
    except Exception as exc:  # pragma: no cover - SDK/preview variance
        logger.debug("invoke_agent_scope fallback to no-op: %s", exc)
        return contextlib.nullcontext()


def execute_tool_scope(
    tool_name: str,
    arguments: Any | None = None,
    conversation_id: str | None = None,
):
    """Context manager wrapping a single tool/MCP call (ExecuteToolScope)."""
    if not config.ENABLE_A365_OBSERVABILITY:
        return contextlib.nullcontext()
    try:
        from microsoft_agents_a365.observability.core import (
            ExecuteToolScope,
            ToolCallDetails,
            TenantDetails,
            ToolType,
        )

        args_str = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
        return ExecuteToolScope(
            details=ToolCallDetails(
                tool_name=tool_name,
                arguments=args_str,
                tool_type=getattr(ToolType.FUNCTION, "value", None),
            ),
            agent_details=_agent_details(conversation_id),
            tenant_details=TenantDetails(tenant_id=config.AGENT_TENANT_ID or "unknown"),
        )
    except Exception as exc:  # pragma: no cover - SDK/preview variance
        logger.debug("execute_tool_scope fallback to no-op: %s", exc)
        return contextlib.nullcontext()


async def setup_observability_token(auth: Any, context: Any) -> None:
    """Exchange + cache the agentic-user token the A365 exporter needs.

    Per the Microsoft sample, this runs once per turn: exchange an
    observability-scoped token via the AGENTIC auth handler and cache it so the
    exporter's ``token_resolver`` can authenticate. No-ops cleanly when
    observability is disabled or no live auth/context is present.
    """
    if not (config.ENABLE_A365_OBSERVABILITY and config.ENABLE_A365_OBSERVABILITY_EXPORTER):
        return
    if auth is None or context is None:
        return
    try:
        from microsoft_agents_a365.runtime.environment_utils import (
            get_observability_authentication_scope,
        )

        recipient = getattr(getattr(context, "activity", None), "recipient", None)
        tenant_id = getattr(recipient, "tenant_id", None) or config.AGENT_TENANT_ID or "unknown"
        agent_id = getattr(recipient, "agentic_app_id", None) or config.AGENT_ID or "unknown"

        token_response = await auth.exchange_token(
            context,
            get_observability_authentication_scope(),
            config.AGENTIC_HANDLER_ID,
        )
        token = getattr(token_response, "token", None)
        if token:
            cache_observability_token(tenant_id, agent_id, token)
            logger.debug("Cached A365 observability token (tenant=%s agent=%s).", tenant_id, agent_id)
    except Exception as exc:  # pragma: no cover - depends on live auth/preview SDK
        logger.debug("setup_observability_token skipped: %s", exc)


def force_flush(timeout_millis: int = 10000) -> None:
    """Flush buffered spans (important on shutdown/serverless)."""
    try:
        from opentelemetry import trace as _otel_trace

        _otel_trace.get_tracer_provider().force_flush(timeout_millis)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover
        logger.debug("force_flush skipped: %s", exc)
