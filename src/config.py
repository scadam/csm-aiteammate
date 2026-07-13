"""
Project configuration for the CSM AI Teammate.

All values are read from environment variables (or a local ``.env`` file). No
secrets are hard-coded. The Microsoft 365 Agents SDK consumes the
``CONNECTIONS__*`` / ``AGENTAPPLICATION__*`` variables directly via
``load_configuration_from_env``; this module only holds the project-specific
settings the rest of the codebase needs.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(REPO_ROOT / "data")))

# ---------------------------------------------------------------------------
# Agent identity + manager assignment (each instance has exactly one manager)
# ---------------------------------------------------------------------------

AGENT_MANAGER_USER_ID: str = os.getenv("AGENT__MANAGER__USER_ID", "csm-svasireddy")
AGENT_ID: str = os.getenv("AGENT__IDENTITY__AGENT_ID", "")
AGENT_BLUEPRINT_ID: str = os.getenv("AGENT__IDENTITY__BLUEPRINT_ID", "")
AGENT_DISPLAY_NAME: str = os.getenv("AGENT__DISPLAY_NAME", "CSM AI Teammate")
AGENT_TENANT_ID: str = os.getenv(
    "AGENT__IDENTITY__TENANT_ID",
    os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID", ""),
)

# Microsoft Entra ID Governance access package that governs the agents (created by
# scripts/setup_agent_access_package.ps1). The control plane reads this package +
# its live per-agent assignments (app-only, EntitlementManagement.Read.All) and
# shows the real governance state on the Technical tab. Blank => not surfaced.
ACCESS_PACKAGE_NAME: str = os.getenv(
    "A365__ACCESS_PACKAGE__NAME", "CSM Autopilot - Microsoft 365 Grounding and Governance")
ACCESS_PACKAGE_CATALOG: str = os.getenv("A365__ACCESS_PACKAGE__CATALOG", "CSM Autopilot")
ACCESS_PACKAGE_GROUP: str = os.getenv("A365__ACCESS_PACKAGE__GROUP", "sg-CSM-Autopilot-Agents")

# The agent's own app registration (the "service connection"). These are the
# client credentials the Microsoft 365 Agents SDK already consumes via the
# CONNECTIONS__SERVICE_CONNECTION__* hierarchy; we surface them here so the
# control-plane can acquire an **app-only** Graph token (client credentials) to
# send email as the manager via /users/{manager}/sendMail (needs the Mail.Send
# application permission). Reads (directory facts) keep using DefaultAzureCredential.
SERVICE_CLIENT_ID: str = os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID", "")
SERVICE_CLIENT_SECRET: str = os.getenv("CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTSECRET", "")
SERVICE_TENANT_ID: str = os.getenv(
    "CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID", AGENT_TENANT_ID
)

# OBO auth handler id (configured under AGENTAPPLICATION__USERAUTHORIZATION__HANDLERS__*).
OBO_HANDLER_ID: str = os.getenv("AGENT__OBO__HANDLER_ID", "OBO")
GITHUB_AUTH_HANDLER_ID: str = os.getenv("AGENT__GITHUB__HANDLER_ID", "GITHUB")
# Agentic user-authorization handler (AgenticUserAuthorization) — used to mint the
# agent's own agentic-user token for A365 observability export and for proactive
# 1:1 Teams messages to the manager.
AGENTIC_HANDLER_ID: str = os.getenv("AGENT__AGENTIC__HANDLER_ID", "AGENTIC")
# Delegated Graph scope requested via the agentic-user federation when the agent
# proactively messages its manager in a 1:1 Teams chat.
AGENTIC_USER_GRAPH_SCOPE: str = os.getenv(
    "AGENT__AGENTIC__GRAPH_SCOPE", "https://graph.microsoft.com/.default"
)
# Proactive manager notifications (HITL escalation as a 1:1 Teams message). Off by
# default for local/offline dev; the host turns it on when an agentic identity is live.
ENABLE_MANAGER_NOTIFICATIONS: bool = os.getenv(
    "AGENT__NOTIFICATIONS__ENABLE", "true"
).strip().lower() in {"1", "true", "yes"}

# ---------------------------------------------------------------------------
# Agentic identity — the agent's OWN token (acts AS the agent, not the manager).
# Minted from the blueprint's credentials + the instance identifiers via the
# Microsoft Agents SDK agentic-user federation (``get_agentic_user_token`` — an
# OAuth2 client-credentials/``user_fic`` mint, NOT On-Behalf-Of), so it needs **no
# incoming turn context**. This is what lets the agent act for autonomous /
# system-triggered work (a sweep, a timer, a signal landing) as its own governed
# Entra Agent ID. OBO is still used for anything done **as the manager** (reading
# or writing the manager's own Microsoft 365 data); everything else uses this.
# ---------------------------------------------------------------------------
ENABLE_AGENTIC_IDENTITY: bool = os.getenv(
    "AGENT__AGENTIC_IDENTITY__ENABLE", "true"
).strip().lower() in {"1", "true", "yes"}
# The per-instance Entra app id + agent-user object id this host mints tokens for.
# Optional: when blank, they are resolved per-manager from live instance discovery
# (``agent_instances``); set them to pin a single instance (e.g. the default CSM's).
AGENT_INSTANCE_APP_ID: str = os.getenv("AGENT__IDENTITY__INSTANCE_APP_ID", "")
AGENTIC_USER_ID: str = os.getenv("AGENT__IDENTITY__AGENTIC_USER_ID", "")


# ---------------------------------------------------------------------------
# GitHub Copilot SDK (reasoning loop)
# ---------------------------------------------------------------------------

COPILOT_MODEL: str = os.getenv("COPILOT_MODEL", "gpt-5.4-1")
# Optional client-level GitHub token for the Copilot runtime (dev/local).
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

# Reasoning backend for the Copilot SDK agentic loop:
#   "github" — use the GitHub Copilot identity/runtime (premium requests; needs a token);
#   "azure"  — BYOK Azure OpenAI provider authenticated with MANAGED IDENTITY (no key, no
#              GitHub token). The loop runs on Azure OpenAI (e.g. gpt-5.4-1) and is billed by
#              Azure OpenAI tokens, not GitHub premium requests.
COPILOT_PROVIDER: str = os.getenv("COPILOT_PROVIDER", "azure").strip().lower()
# Data-plane API version for the Azure provider (not the model version).
AZURE_OPENAI_API_VERSION: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
# Let the Copilot SDK discover this repo's skills natively (src/skills/<name>/SKILL.md).
COPILOT_ENABLE_SKILLS: bool = os.getenv("COPILOT_ENABLE_SKILLS", "true").lower() == "true"


def azure_openai_base_url() -> str:
    """The Azure OpenAI resource base URL (strip the ``/openai/v1/`` data-plane suffix)."""
    ep = (AZURE_OPENAI_ENDPOINT or "").rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if ep.endswith(suffix):
            ep = ep[: -len(suffix)]
            break
    return ep


# ---------------------------------------------------------------------------
# Azure OpenAI (NL-to-SQL + constrained draft generation).
# Managed identity ONLY (DefaultAzureCredential) — never key-based.
# NOT Snowflake Cortex.
# ---------------------------------------------------------------------------

AZURE_OPENAI_ENDPOINT: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
"""Azure OpenAI /openai/v1/ endpoint, e.g. https://<resource>.openai.azure.com/openai/v1/"""

AZURE_OPENAI_SCOPE: str = os.getenv(
    "AZURE_OPENAI_SCOPE", "https://cognitiveservices.azure.com/.default"
)
"""AAD scope for the bearer-token provider used by the Azure OpenAI client."""

AZURE_TENANT_ID: str = os.getenv("AZURE_TENANT_ID", AGENT_TENANT_ID)
"""Tenant to pin DefaultAzureCredential to (defaults to the agent tenant). Keeps the
runtime on the intended demo tenant rather than a guest/home tenant."""

OPENAI_SQL_MODEL: str = os.getenv("AZURE_OPENAI_SQL_DEPLOYMENT", "gpt-5.4-1")
OPENAI_DRAFT_MODEL: str = os.getenv("AZURE_OPENAI_DRAFT_DEPLOYMENT", "gpt-5.4-1")

# ---------------------------------------------------------------------------
# Snowflake (the relational back end for NL-to-SQL).
# When SNOWFLAKE_ACCOUNT is set, queries run against real Snowflake; otherwise
# the in-memory SQLite simulation (seeded from data/*.json) is used. Snowflake
# auth is key-pair (RSA) — the runtime role is read-only; a separate admin role
# is used only by the data loader to create the database/schema and grant access.
# ---------------------------------------------------------------------------

SNOWFLAKE_ACCOUNT: str = os.getenv("SNOWFLAKE_ACCOUNT", "")
SNOWFLAKE_USER: str = os.getenv("SNOWFLAKE_USER", "")
SNOWFLAKE_PASSWORD: str = os.getenv("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_PRIVATE_KEY_PATH: str = os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH", "")
SNOWFLAKE_PRIVATE_KEY: str = os.getenv("SNOWFLAKE_PRIVATE_KEY", "")
SNOWFLAKE_PRIVATE_KEY_PASSPHRASE: str = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
SNOWFLAKE_DATABASE: str = os.getenv("SNOWFLAKE_DATABASE", "CSM_DB")
SNOWFLAKE_SCHEMA: str = os.getenv("SNOWFLAKE_SCHEMA", "ADOPTION")
SNOWFLAKE_WAREHOUSE: str = os.getenv("SNOWFLAKE_WAREHOUSE", "GIM_WH")
SNOWFLAKE_ROLE: str = os.getenv("SNOWFLAKE_ROLE", "GIM_AGENT_ROLE")
# Privileged role used ONLY by scripts/load_data.py for DDL + grants (not at runtime).
SNOWFLAKE_ADMIN_ROLE: str = os.getenv("SNOWFLAKE_ADMIN_ROLE", "SYSADMIN")

USE_SNOWFLAKE: bool = bool(SNOWFLAKE_ACCOUNT)
"""True when a real Snowflake account is configured; otherwise SQLite simulation is used."""

# ---------------------------------------------------------------------------
# A365 Tooling Gateway + Work IQ MCP (Microsoft 365 grounding — REAL).
# Work IQ is a remote MCP server (host workiq.svc.cloud.microsoft) reached on the
# manager's behalf via OBO. It is delegated-only (no app-only); the agent must
# carry the manager OBO token scoped to WORKIQ_SCOPE. When WORKIQ_MCP_ENDPOINT is
# unset, the tools fall back to the offline JSON fixture (local dev only).
# ---------------------------------------------------------------------------

TOOLING_GATEWAY_URL: str = os.getenv("A365__TOOLING_GATEWAY__URL", "")
TOOLING_GATEWAY_REGISTRATION_ID: str = os.getenv("A365__TOOLING_GATEWAY__REGISTRATION_ID", "")

WORKIQ_MCP_ENDPOINT: str = os.getenv("WORKIQ__MCP__ENDPOINT", "")
"""Remote Work IQ MCP server URL (e.g. https://workiq.svc.cloud.microsoft/mcp)."""
WORKIQ_APP_ID: str = os.getenv("WORKIQ__APP_ID", "fdcc1f02-fc51-4226-8753-f668596af7f7")
"""Work IQ API Entra application id (well-known)."""
WORKIQ_SCOPE: str = os.getenv("WORKIQ__SCOPE", "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask")
"""Delegated OAuth scope requested via OBO to call Work IQ on the manager's behalf."""

USE_WORKIQ: bool = bool(WORKIQ_MCP_ENDPOINT)
"""True when a real Work IQ MCP endpoint is configured; otherwise offline JSON fallback."""

# ---------------------------------------------------------------------------
# Gainsight NXT (Customer Success + PX).
# Simulated-real: the REST contracts are real (paths, accesskey header, response
# envelopes) but served in-process from the JSON fixtures (no live Gainsight
# tenant). When a real domain + access key are configured, the client can be
# pointed at the live REST API without changing tool logic.
# ---------------------------------------------------------------------------

GAINSIGHT_DOMAIN: str = os.getenv("GAINSIGHT__DOMAIN", "https://companyapi.gainsightcloud.com")
GAINSIGHT_ACCESS_KEY: str = os.getenv("GAINSIGHT__ACCESS_KEY", "simulated-access-key")
GAINSIGHT_PX_API_KEY: str = os.getenv("GAINSIGHT__PX_API_KEY", "simulated-px-key")
GAINSIGHT_LIVE: bool = os.getenv("GAINSIGHT__LIVE", "false").lower() == "true"
"""When true, call the real Gainsight REST API; otherwise use the in-process simulation."""

# Custom (BYO) MCP server registered on the A365 Tooling Gateway. When set, the
# agent's reasoning loop consumes the custom tools through the gateway endpoint
# (governed) rather than the raw local MCP endpoint.
MCP_GATEWAY_ENDPOINT: str = os.getenv("A365__TOOLING_GATEWAY__MCP_ENDPOINT", "")
MCP_PUBLIC_URL: str = os.getenv("MCP__PUBLIC_URL", "")
"""Publicly reachable URL of this agent's MCP server (for BYO registration)."""
MCP_SERVER_NAME: str = os.getenv("MCP__SERVER_NAME", "ext_CsmTeammate")

# ---------------------------------------------------------------------------
# Microsoft Purview — Data Security & Governance (DSPM for AI).
# The agent acts on behalf of its manager, so the MANAGER is the user. Prompts
# (uploadText) and responses (downloadText) are evaluated by Purview via the
# Microsoft Graph DSPM API using the manager's delegated (OBO) token — real DLP
# policy enforcement + audit. Requires delegated Graph permissions
# Content.Process.User + ProtectionScopes.Compute.User. When no Graph token is
# available (offline/dev), the client records to a local ledger and runs the
# local SIT scanner so the governance dashboard still works.
# ---------------------------------------------------------------------------

GRAPH_BASE_URL: str = os.getenv("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0")
GRAPH_SCOPE: str = os.getenv("GRAPH_SCOPE", "https://graph.microsoft.com/.default")
"""Delegated Graph scope requested via OBO for Purview DSPM calls."""

PURVIEW_APP_LOCATION_ID: str = os.getenv(
    "PURVIEW__APP_LOCATION_ID", os.getenv("AGENT__IDENTITY__BLUEPRINT_ID", "")
)
"""Entra application id identifying this agent as the policyLocationApplication in
Purview (defaults to the agent blueprint app id)."""

PURVIEW_APP_NAME: str = os.getenv("PURVIEW__APP_NAME", "CSM Autopilot")
PURVIEW_APP_VERSION: str = os.getenv("PURVIEW__APP_VERSION", "1.0.0")
ENABLE_PURVIEW: bool = os.getenv("ENABLE_PURVIEW", "true").lower() == "true"
"""Master switch for Purview DSPM integration (local ledger + SIT scan run either
way; real Graph calls happen only when a manager OBO token is present)."""

# When true, every agent/MCP tool call is logged to Purview DSPM as a real
# processContent "Tool call" event (so MCP tool-call activity is visible in DSPM
# Activity explorer + the governance dashboard). Set false to reduce processContent
# volume/cost if needed.
PURVIEW_LOG_TOOL_CALLS: bool = os.getenv("PURVIEW__LOG_TOOL_CALLS", "true").lower() == "true"

# Name of the Purview DLP-for-AI policy scoped to this agent's Entra app (created by
# scripts/setup_purview_dlp.ps1). Used only to surface posture on the security
# dashboard — the actual enforcement is done by Purview returning a restrictAccess
# action that the agent honours via processContent.
PURVIEW_DLP_POLICY: str = os.getenv("PURVIEW__DLP_POLICY", "")
# Display name of the custom sensitive information type that flags a customer
# confidential identifier (account id / "CUSTOMER-CONFIDENTIAL"). Aligns the agent-side
# cross-customer fence with the Purview DLP rule.
PURVIEW_CONFIDENTIAL_SIT: str = os.getenv("PURVIEW__CONFIDENTIAL_SIT", "Customer Confidential ID")

# ---------------------------------------------------------------------------
# Durable cost ledger (Azure Table Storage via managed identity, no shared key).
# When set, per-job inference cost points are persisted so the cost/token history
# survives container recycles. Blank => in-memory only. See control_plane/cost_store.py.
# ---------------------------------------------------------------------------
COST_STORE_TABLE_ENDPOINT: str = os.getenv("COST_STORE__TABLE_ENDPOINT", "")

# ---------------------------------------------------------------------------
# Agent memory (the agent "learns" through a maintained memory file).
# ---------------------------------------------------------------------------

MEMORY_DIR: Path = Path(os.getenv("MEMORY_DIR", str(REPO_ROOT / "data" / "memory")))
MEMORY_MAX_CHARS: int = int(os.getenv("MEMORY_MAX_CHARS", "6000"))
"""Soft cap on a manager's memory.md; the reflector condenses when exceeded."""
ENABLE_MEMORY_REFLECTION: bool = os.getenv("ENABLE_MEMORY_REFLECTION", "true").lower() == "true"
"""When true, the agent runs an Azure OpenAI reflection after each job to update memory."""

# ---------------------------------------------------------------------------
# Observability (OTEL + A365 Observability SDK)
# ---------------------------------------------------------------------------

SERVICE_NAME: str = os.getenv("OTEL_SERVICE_NAME", "csm_ai_teammate")
SERVICE_NAMESPACE: str = os.getenv("OTEL_SERVICE_NAMESPACE", "csm.autopilot")
OTEL_EXPORTER_OTLP_ENDPOINT: str = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

ENABLE_A365_OBSERVABILITY: bool = os.getenv("ENABLE_A365_OBSERVABILITY", "false").lower() == "true"
ENABLE_A365_OBSERVABILITY_EXPORTER: bool = (
    os.getenv("ENABLE_A365_OBSERVABILITY_EXPORTER", "false").lower() == "true"
)
A365_CLUSTER_CATEGORY: str = os.getenv("A365_CLUSTER_CATEGORY", "prod")
# Base host of the Agent 365 observability service. The SDK builds the per-tenant/
# per-agent OTLP export URL from this; surfaced read-only in the technical view.
A365_OBSERVABILITY_HOST: str = os.getenv(
    "A365_OBSERVABILITY_HOST", "https://agent365.svc.cloud.microsoft"
)

# ---------------------------------------------------------------------------
# Control plane (dashboards) behaviour
# ---------------------------------------------------------------------------

# When false (default), the CSM review queue only shows REAL outcomes the agent has
# produced this session — no pre-seeded fixture items. The seeded queue is a demo aid
# only; keep it off so the cockpit never shows review items that don't really exist.
SEED_REVIEW_QUEUE: bool = os.getenv("CONTROL_PLANE__SEED_REVIEW_QUEUE", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Behavioural defaults
# ---------------------------------------------------------------------------

# Signal Detection Agent: only act on signals at or above this severity score (1-5).
SIGNAL_SEVERITY_THRESHOLD: int = int(os.getenv("SIGNAL_SEVERITY_THRESHOLD", "3"))

# aiohttp host port.
PORT: int = int(os.getenv("PORT", "3978"))

# MCP server (this agent's tools exposed over MCP).
MCP_HOST: str = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT: int = int(os.getenv("MCP_PORT", "8000"))
