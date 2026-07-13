"""
Azure OpenAI tool-calling reasoning loop (Linux/hosted path).

The GitHub Copilot SDK wheel is Windows-only, so it cannot run in the Linux
Container App that hosts the agent. Mirroring the proven ``ess-mcp/demo_agent``
pattern (which reasons via the OpenAI SDK, not the Copilot wheel), this module
drives the agent's reasoning with **Azure OpenAI** (managed identity, never a
key) using function/tool-calling over the *same* ``src.tools.TOOL_SPECS`` that
back the Copilot and MCP surfaces — so behaviour stays consistent across hosts.

Tools execute in-process within the per-turn :class:`~src.identity.RequestContext`
set by the agent, so manager resolution and On-Behalf-Of token exchange work
exactly as they do on the Copilot path.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import config, observability
from . import openai_client
from .persona import build_persona
from .tools import TOOL_SPECS

logger = logging.getLogger(__name__)

_SPECS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}
_MAX_STEPS = 8

# Per-conversation history so the hosted (Azure OpenAI) path is multi-turn, exactly
# like the Copilot session path. Keyed by ``manager_id:conversation_id``. In-memory
# only (per process), mirroring the Copilot session store; trimmed to the last N
# user/assistant messages so context (e.g. "action these") survives across turns.
_histories: dict[str, list[dict]] = {}
_history_locks: dict[str, asyncio.Lock] = {}
_MAX_HISTORY_MESSAGES = 16  # ~8 user/assistant exchanges retained per conversation


def _history_lock(session_key: str) -> asyncio.Lock:
    """Serialise turns for one conversation so shared history can't interleave."""
    lock = _history_locks.get(session_key)
    if lock is None:
        lock = asyncio.Lock()
        _history_locks[session_key] = lock
    return lock


def _openai_tools() -> list[dict]:
    """Build the OpenAI function-tool schemas from the shared ToolSpec registry."""
    tools: list[dict] = []
    for spec in TOOL_SPECS:
        schema = spec.params_model.model_json_schema()
        # OpenAI function parameters must be a JSON-schema object.
        schema.pop("title", None)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": schema,
                },
            }
        )
    return tools


async def _dispatch(name: str, arguments: str) -> str:
    """Execute a single tool call, traced with an A365 ExecuteToolScope."""
    spec = _SPECS_BY_NAME.get(name)
    if spec is None:
        return f"Error: unknown tool '{name}'."
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return f"Error: could not parse arguments for {name}: {exc}"
    try:
        with observability.execute_tool_scope(tool_name=name, arguments=args):
            result = await spec.func(**args)
        text = result if isinstance(result, str) else json.dumps(result)
        # Log the tool CALL to Purview DSPM (real processContent on the manager's OBO
        # token in this turn context) so the bot path's MCP/agent tool calls are
        # visible in DSPM too. Best-effort: never breaks the turn.
        try:
            from . import purview, identity
            mgr = identity.resolve_manager() or {}
            await purview.log_tool_call(
                tool=name, surface="Agent tool",
                manager={"id": mgr.get("manager_id"), "name": mgr.get("display_name"),
                         "entra_object_id": mgr.get("entra_object_id")},
                arguments=args, result=text)
        except Exception:  # pragma: no cover
            logger.debug("reasoning tool-call DSPM logging skipped", exc_info=True)
        return text
    except Exception as exc:  # surface tool errors back to the model
        logger.exception("Tool %s failed", name)
        return f"Error executing {name}: {exc}"


def _complete(messages: list[dict], tools: list[dict]):
    return openai_client.chat_completion(
        model=config.COPILOT_MODEL,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        temperature=0.2,
        max_tokens=1200,
    )


async def _run_loop(messages: list[dict], tools: list[dict]) -> str:
    """Drive the tool-calling loop over ``messages`` and return the final text."""
    for _ in range(_MAX_STEPS):
        response = await asyncio.to_thread(_complete, messages, tools)
        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        if not tool_calls:
            return msg.content or "I don't have anything to add."

        # Record the assistant's tool-call request, then run each tool.
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments or "{}",
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc in tool_calls:
            result = await _dispatch(tc.function.name, tc.function.arguments or "{}")
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )

    logger.warning("Reasoning loop hit the %d-step cap.", _MAX_STEPS)
    return "I've gathered what I can, but the request needed more steps than expected. Could you narrow it down?"


async def run_turn(user_text: str, session_key: str | None = None) -> str:
    """
    Run one agentic turn: reason with Azure OpenAI, executing tools as requested,
    and return the final assistant text. Must be called inside the agent's
    per-turn RequestContext so tools can resolve the manager and do OBO exchange.

    When ``session_key`` (``manager_id:conversation_id``) is given, prior turns for
    that conversation are prepended so the agent can follow up ("action these",
    "do the first one") — matching the multi-turn Copilot session path. History is
    per-process and scoped to the conversation, so one manager's context never
    leaks to another.
    """
    tools = _openai_tools()
    lock = _history_lock(session_key) if session_key else None
    if lock:
        await lock.acquire()
    try:
        history = list(_histories.get(session_key, [])) if session_key else []
        messages: list[dict] = [
            {"role": "system", "content": build_persona()},
            *history,
            {"role": "user", "content": user_text},
        ]
        final_text = await _run_loop(messages, tools)
        if session_key is not None:
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": final_text})
            _histories[session_key] = history[-_MAX_HISTORY_MESSAGES:]
        return final_text
    finally:
        if lock:
            lock.release()
