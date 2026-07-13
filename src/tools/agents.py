"""
Work IQ Copilot tools — REAL: ``ask`` and ``list_agents``.

These map to the Work IQ MCP ``ask`` (invoke Microsoft 365 Copilot for
natural-language reasoning over the manager's work data) and ``list_agents``
(discover available Copilot agents) tools, carrying the manager's OBO token.
When no Work IQ endpoint is configured they fall back to an offline response for
local development.
"""

from __future__ import annotations

import json
import logging

from .. import config, identity, workiq_client
from ..observability import execute_tool_scope
from . import workiq

logger = logging.getLogger(__name__)

_OFFLINE_AGENTS = [
    {"agentId": "bizchat-as-gpt-scenario", "name": "Microsoft 365 Copilot", "provider": "Microsoft"},
]


async def ask(question: str, account_id: str = "") -> str:
    """Invoke Microsoft 365 Copilot reasoning over the manager's work data (Work IQ ``ask``)."""
    with execute_tool_scope("workiq.ask", {"question": question, "account_id": account_id}):
        if not config.USE_WORKIQ:
            grounding = await workiq.search_microsoft_365(question, account_id=account_id)
            return f"[offline] Microsoft 365 Copilot answer for: {question}\n\n{grounding}"

        token = await identity.exchange_obo_token([config.WORKIQ_SCOPE])
        if not token:
            return "Could not acquire a Work IQ token on the manager's behalf (OBO required)."

        q = question if not account_id else f"{question} (for account {account_id})"
        try:
            return await workiq_client.call_tool("ask", {"question": q}, token)
        except workiq_client.WorkIQError as exc:
            logger.warning("Work IQ ask failed: %s", exc)
            return f"Work IQ is unavailable right now: {exc}"


async def list_agents() -> str:
    """List the Microsoft 365 Copilot agents available to the manager (Work IQ ``list_agents``)."""
    with execute_tool_scope("workiq.list_agents", {}):
        if not config.USE_WORKIQ:
            return json.dumps(_OFFLINE_AGENTS, indent=2)

        token = await identity.exchange_obo_token([config.WORKIQ_SCOPE])
        if not token:
            return "Could not acquire a Work IQ token on the manager's behalf (OBO required)."
        try:
            return await workiq_client.call_tool("list_agents", {}, token)
        except workiq_client.WorkIQError as exc:
            logger.warning("Work IQ list_agents failed: %s", exc)
            return f"Work IQ is unavailable right now: {exc}"
