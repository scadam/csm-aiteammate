"""Truthful readiness for Agent 365, identity, telemetry, MCP, model, and state."""

from __future__ import annotations

import os
from typing import Any

from . import config


def snapshot() -> dict[str, Any]:
    checks = {
        "agentSdk": {
            "ready": bool(os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID")),
            "detail": "Microsoft 365 Agents SDK service connection",
        },
        "managerObo": {
            "ready": bool(config.OBO_HANDLER_ID),
            "detail": "Manager OBO handler configured; live exchange requires a signed-in turn",
        },
        "agenticUser": {
            "ready": bool(
                config.AGENT_TENANT_ID
                and config.AGENT_INSTANCE_APP_ID
                and config.AGENTIC_USER_ID
            ),
            "detail": "Autonomous Agent ID identity",
        },
        "a365Observability": {
            "ready": bool(
                config.ENABLE_A365_OBSERVABILITY
                and config.ENABLE_A365_OBSERVABILITY_EXPORTER
                and config.AGENT_ID
                and config.AGENT_TENANT_ID
            ),
            "detail": "Authenticated Agent 365 telemetry export",
        },
        "reasoning": {
            "ready": bool(config.AZURE_OPENAI_ENDPOINT),
            "detail": "Azure OpenAI with managed identity",
        },
        "mcpFacade": {
            "ready": bool(
                config.MCP_ALLOW_DEV_NO_AUTH
                or (
                    config.MCP_TOKEN_ISSUER
                    and config.MCP_TOKEN_AUDIENCE
                    and config.MCP_JWKS_URL
                    and config.MCP_REQUIRED_SCOPE
                    and config.MCP_RESOURCE_SERVER_URL
                )
            ),
            "detail": "FastMCP facade authentication",
        },
        "toolingGateway": {
            "ready": bool(
                os.getenv(config.SPEC.mcp_exposure.tooling_gateway.endpoint_env)
                and os.getenv(config.SPEC.mcp_exposure.tooling_gateway.registration_id_env)
            ),
            "detail": "Agent 365 Tooling Gateway registration",
        },
        "stateStore": {
            "ready": bool(config.DEVELOPMENT_MODE or config.STATE_TABLE_ENDPOINT),
            "detail": (
                "SQLite development store"
                if config.DEVELOPMENT_MODE
                else "Managed-identity Azure Table shared state"
            ),
        },
    }
    for server in config.SPEC.mcp_servers:
        checks[f"mcp:{server.id}"] = {
            "ready": bool(os.getenv(server.endpoint_env)),
            "detail": f"Remote MCP endpoint {server.endpoint_env}",
        }
    for source in config.SPEC.openapi_sources:
        checks[f"openapi:{source.id}"] = {
            "ready": bool(os.getenv(source.base_url_env)),
            "detail": f"OpenAPI base URL {source.base_url_env}",
        }
    live_ready = all(check["ready"] for check in checks.values())
    if config.OFFLINE_MODE:
        status = "offline"
    elif live_ready:
        status = "ready"
    else:
        status = "degraded"
    return {"status": status, "liveReady": live_ready, "checks": checks}
