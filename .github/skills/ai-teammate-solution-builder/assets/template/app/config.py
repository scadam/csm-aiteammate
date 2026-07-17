"""Environment-driven runtime configuration shared by all generated processes."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from .spec import load_spec


load_dotenv()
SPEC = load_spec()


def _boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


OFFLINE_MODE = _boolean("AI_TEAMMATE_OFFLINE", SPEC.identity.development_mode)
DEVELOPMENT_MODE = _boolean("AI_TEAMMATE_DEVELOPMENT_MODE", SPEC.identity.development_mode)
AGENT_DISPLAY_NAME = os.getenv("AGENT__DISPLAY_NAME", SPEC.agent.display_name)
AGENT_MANAGER_ID = os.getenv("AGENT__MANAGER__USER_ID", SPEC.identity.default_manager_id)
AGENT_ID = os.getenv("AGENT__IDENTITY__AGENT_ID", "")
AGENT_BLUEPRINT_ID = os.getenv("AGENT__IDENTITY__BLUEPRINT_ID", "")
AGENT_TENANT_ID = os.getenv("AGENT__IDENTITY__TENANT_ID", "")
AGENT_INSTANCE_APP_ID = os.getenv("AGENT__IDENTITY__INSTANCE_APP_ID", "")
AGENTIC_USER_ID = os.getenv("AGENT__IDENTITY__AGENTIC_USER_ID", "")
OBO_HANDLER_ID = os.getenv("AGENT__OBO__HANDLER_ID", SPEC.identity.manager_obo.handler_id)
AGENTIC_HANDLER_ID = os.getenv("AGENT__AGENTIC__HANDLER_ID", SPEC.identity.agentic_user.handler_id)
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_SCOPE = os.getenv("AZURE_OPENAI_SCOPE", "https://cognitiveservices.azure.com/.default")
MODEL = os.getenv(SPEC.agent.reasoning.model_env, SPEC.agent.reasoning.default_model)
ENABLE_A365_OBSERVABILITY = _boolean("ENABLE_A365_OBSERVABILITY")
ENABLE_A365_OBSERVABILITY_EXPORTER = _boolean("ENABLE_A365_OBSERVABILITY_EXPORTER")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv(SPEC.runtime.mcp_host.port_env, str(SPEC.runtime.mcp_host.default_port)))
MCP_ALLOW_DEV_NO_AUTH = _boolean("MCP_ALLOW_DEV_NO_AUTH", OFFLINE_MODE and DEVELOPMENT_MODE)
MCP_TOKEN_ISSUER = os.getenv("MCP_TOKEN_ISSUER", "")
MCP_TOKEN_AUDIENCE = os.getenv("MCP_TOKEN_AUDIENCE", "")
MCP_JWKS_URL = os.getenv("MCP_JWKS_URL", "")
MCP_REQUIRED_SCOPE = os.getenv("MCP_REQUIRED_SCOPE", "")
MCP_RESOURCE_SERVER_URL = os.getenv("MCP_RESOURCE_SERVER_URL", "")
CONTROL_PLANE_TOKEN_ISSUER = os.getenv("CONTROL_PLANE_TOKEN_ISSUER", "")
CONTROL_PLANE_TOKEN_AUDIENCE = os.getenv("CONTROL_PLANE_TOKEN_AUDIENCE", "")
CONTROL_PLANE_JWKS_URL = os.getenv("CONTROL_PLANE_JWKS_URL", "")
CONTROL_PLANE_REQUIRED_SCOPE = os.getenv(
    "CONTROL_PLANE_REQUIRED_SCOPE", SPEC.teams_app.sso.delegated_scope
)
CONTROL_PLANE_ALLOWED_CLIENT_IDS = {
    value.strip()
    for value in os.getenv("CONTROL_PLANE_ALLOWED_CLIENT_IDS", "").split(",")
    if value.strip()
}
MCP_ALLOWED_CLIENT_IDS = {
    value.strip()
    for value in os.getenv("MCP_ALLOWED_CLIENT_IDS", "").split(",")
    if value.strip()
}
MAX_HTTP_RESPONSE_BYTES = int(os.getenv("MAX_HTTP_RESPONSE_BYTES", "1048576"))
STATE_TABLE_ENDPOINT = os.getenv("STATE_TABLE_ENDPOINT", "")
STATE_TABLE_NAME = os.getenv("STATE_TABLE_NAME", "aiteammatestate")
