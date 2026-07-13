"""
Register this agent's MCP server as a **BYO (bring-your-own) MCP server** on the
Agent 365 Tooling Gateway.

Per the Microsoft documentation, a remote MCP server is registered with Agent 365
using the ``a365 develop-mcp register-external-mcp-server`` CLI command (or a JSON
file). After registration an IT admin reviews and approves it in the Microsoft
365 admin center; once approved + consented, Agent 365 routes all invocations
through the **Tooling Gateway**, and supported clients (incl. GitHub Copilot)
call the gateway endpoint — never the raw MCP endpoint.

This module:
- builds the BYO registration document (:func:`build_registration_document`),
- writes it to disk (:func:`write_registration_file`), and
- invokes the CLI (:func:`register_byo_mcp_server`),

using **EntraOAuth** with the blueprint's exposed scope
(``api://<blueprint-app-id>/access_agent_as_user``) so the gateway brokers tokens.

Reference:
https://learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-tools-for-agent#bring-your-own-byo-mcp-server
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..tools import TOOL_SPECS

logger = logging.getLogger(__name__)

# BYO server names must start with "ext_" and be <= 20 chars.
_DEFAULT_SERVER_NAME = "ext_CsmTeammate"


@dataclass
class ByoRegistration:
    server_name: str
    server_url: str
    auth_type: str
    remote_scopes: str
    publisher: str
    description: str
    tool_names: list[str]


def _server_name() -> str:
    name = config.MCP_SERVER_NAME or _DEFAULT_SERVER_NAME
    if not name.startswith("ext_"):
        name = "ext_" + name
    return name[:20]


def build_registration() -> ByoRegistration:
    """Build the BYO MCP registration descriptor from configuration."""
    remote_scope = (
        f"api://{config.AGENT_BLUEPRINT_ID or config.AGENT_ID}/access_agent_as_user"
        if (config.AGENT_BLUEPRINT_ID or config.AGENT_ID)
        else ""
    )
    return ByoRegistration(
        server_name=_server_name(),
        server_url=config.MCP_PUBLIC_URL or f"http://{config.MCP_HOST}:{config.MCP_PORT}/mcp",
        auth_type="EntraOAuth",
        remote_scopes=remote_scope,
        publisher="CSM Autopilot",
        description="Digital CSM teammate: Snowflake, Gainsight, KB, content build.",
        tool_names=[spec.name for spec in TOOL_SPECS],
    )


def build_registration_document() -> dict:
    """Build the JSON document accepted by ``register-external-mcp-server -f``."""
    reg = build_registration()
    return {
        "serverName": reg.server_name,
        "serverUrl": reg.server_url,
        "authType": reg.auth_type,
        "description": reg.description,
        "publisherName": reg.publisher,
        "tools": [{"name": spec.name, "description": spec.description} for spec in TOOL_SPECS],
        "remoteScopes": reg.remote_scopes or None,
        "externalOAuth": None,
        "apiKey": None,
    }


def write_registration_file(path: str | Path = "byo-mcp-registration.json") -> Path:
    """Write the BYO registration JSON document to disk for ``a365 ... -f <file>``."""
    out = Path(path)
    out.write_text(json.dumps(build_registration_document(), indent=2), encoding="utf-8")
    logger.info("Wrote BYO MCP registration document to %s", out)
    return out


def build_cli_command(input_file: str | Path | None = None) -> list[str]:
    """Build the ``a365 develop-mcp register-external-mcp-server`` command."""
    if input_file is not None:
        return ["a365", "develop-mcp", "register-external-mcp-server", "-f", str(input_file)]
    reg = build_registration()
    cmd = [
        "a365", "develop-mcp", "register-external-mcp-server",
        "--server-name", reg.server_name,
        "--server-url", reg.server_url,
        "--auth-type", reg.auth_type,
        "--publisher", reg.publisher,
        "--description", reg.description,
        "--tools", ",".join(reg.tool_names),
    ]
    if reg.remote_scopes:
        cmd += ["--remote-scopes", reg.remote_scopes]
    return cmd


def register_byo_mcp_server(use_file: bool = True, dry_run: bool = False) -> int:
    """
    Register the MCP server as a BYO MCP server via the ``a365`` CLI.

    Requires a **publicly reachable** ``MCP__PUBLIC_URL``. Returns the CLI exit
    code (0 on success). After registration, an IT admin must approve the server
    in the Microsoft 365 admin center before agents can use it via the gateway.
    """
    reg = build_registration()
    if not reg.server_url.startswith("https://"):
        logger.warning(
            "BYO MCP registration needs a public HTTPS URL (MCP__PUBLIC_URL); got %s. "
            "Expose the MCP server (e.g. via a tunnel) before registering.",
            reg.server_url,
        )
    input_file = write_registration_file() if use_file else None
    cmd = build_cli_command(input_file)
    if dry_run:
        cmd.append("--dry-run")
    logger.info("Registering BYO MCP server: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603 - trusted CLI
    logger.info("a365 register-external-mcp-server exit=%s\n%s\n%s",
                result.returncode, result.stdout, result.stderr)
    return result.returncode


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    write_registration_file()

