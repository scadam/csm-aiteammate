"""
Real Work IQ MCP client (Microsoft 365 grounding).

Connects to the remote **Work IQ MCP server** over streamable HTTP and invokes
its generic tools (``ask``, ``list_agents``, ``fetch``, ``do_action``,
``call_function``, …) on the **manager's behalf**. Work IQ is Microsoft Entra
delegated-only — every call must carry the manager's On-Behalf-Of (OBO) access
token scoped to ``config.WORKIQ_SCOPE`` (``api://workiq.svc.cloud.microsoft/
WorkIQAgent.Ask``). Application-only tokens are not supported.

The endpoint and scope are configuration-driven (never hard-coded), so the
Public-Preview contract can change without touching tool logic.

References:
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/overview
- https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import config

logger = logging.getLogger(__name__)


class WorkIQError(RuntimeError):
    """Raised when a Work IQ MCP call cannot be made or returns an error."""


def _extract_text(result: Any) -> str:
    """Extract a human-readable string from an MCP CallToolResult."""
    # Structured content (preferred when present).
    structured = getattr(result, "structuredContent", None)
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    if structured and not parts:
        return json.dumps(structured, indent=2, default=str)
    body = "\n".join(parts) if parts else "(no content)"
    if structured:
        body = f"{body}\n\n{json.dumps(structured, indent=2, default=str)}"
    return body


async def call_tool(tool_name: str, arguments: dict, obo_token: str) -> str:
    """
    Invoke a Work IQ MCP tool with the manager's OBO token.

    Raises WorkIQError if the endpoint is not configured or the call fails.
    """
    if not config.WORKIQ_MCP_ENDPOINT:
        raise WorkIQError("WORKIQ__MCP__ENDPOINT is not configured.")
    if not obo_token:
        raise WorkIQError("No Work IQ OBO token available for the manager.")

    headers = {"Authorization": f"Bearer {obo_token}"}
    try:
        async with streamablehttp_client(config.WORKIQ_MCP_ENDPOINT, headers=headers) as (
            read_stream,
            write_stream,
            _get_session_id,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
    except WorkIQError:
        raise
    except Exception as exc:  # network / protocol / auth errors
        raise WorkIQError(f"Work IQ MCP call '{tool_name}' failed: {exc}") from exc

    if getattr(result, "isError", False):
        raise WorkIQError(f"Work IQ tool '{tool_name}' returned an error: {_extract_text(result)}")
    return _extract_text(result)
