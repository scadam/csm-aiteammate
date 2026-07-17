"""Optional GitHub Copilot SDK runtime over generated skills and shared tools."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from copilot import CopilotClient, PermissionHandler, define_tool

from . import agent_identity, config
from .capabilities import CapabilityRegistry, ToolSpec
from .reasoning import system_message


_client: CopilotClient | None = None
_sessions: dict[str, Any] = {}
_lock = asyncio.Lock()


def _copilot_tool(tool: ToolSpec, registry: CapabilityRegistry):
    async def handler(params, _invocation):
        arguments = params.model_dump(exclude_none=True)
        current = agent_identity.current_context()
        context = {
            "manager": {"id": current.manager_id} if current else {},
            "conversationId": current.conversation_id if current else "",
            "idempotencyKey": (
                f"turn:{current.session_key}:{current.activity_id}"
                if current and current.activity_id
                else ""
            ),
        }
        result = await registry.dispatch_tool(
            tool.name, arguments, context=context, surface="agent"
        )
        return result.model_dump_json()

    return define_tool(
        tool.name,
        description=tool.description,
        handler=handler,
        params_type=tool.params_model,
    )


async def get_session(session_key: str, registry: CapabilityRegistry):
    global _client
    async with _lock:
        if session_key in _sessions:
            return _sessions[session_key]
        if not config.DEVELOPMENT_MODE:
            raise RuntimeError(
                "GitHub Copilot SDK production use requires an explicit permission policy adapter"
            )
        if _client is None:
            _client = CopilotClient(
                github_token=os.getenv("GITHUB_TOKEN") or None,
                use_logged_in_user=not bool(os.getenv("GITHUB_TOKEN")),
            )
            await _client.start()
        session = await _client.create_session(
            model=config.MODEL,
            on_permission_request=PermissionHandler.approve_all,
            tools=[_copilot_tool(tool, registry) for tool in registry.tool_specs("agent")],
            streaming=False,
            system_message={"mode": "append", "content": system_message(registry)},
            enable_skills=True,
            skill_directories=[
                str(Path(__file__).resolve().parent / "generated_skills")
            ],
        )
        _sessions[session_key] = session
        return session


async def run_turn(user_text: str, *, session_key: str, registry: CapabilityRegistry) -> str:
    session = await get_session(session_key, registry)
    event = await session.send_and_wait(user_text, timeout=120.0)
    data = getattr(event, "data", None) if event else None
    return getattr(data, "content", None) or "The Copilot session returned no response."
