"""Bounded, conversation-isolated reasoning over the shared capability registry."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import config
from .capabilities import CapabilityRegistry
from .model_client import complete


_histories: dict[str, list[dict[str, Any]]] = {}
_locks: dict[str, asyncio.Lock] = {}
_MAX_HISTORY = 16


def system_message(registry: CapabilityRegistry) -> str:
    return (
        f"You are {config.SPEC.agent.display_name}, {config.SPEC.agent.role}.\n"
        f"{config.SPEC.agent.instructions}\n\n{registry.skills.catalog_markdown()}\n\n"
        "Load a relevant skill with get_skill before specialist work. Never claim an "
        "offline result is live. Never bypass review, identity, or idempotency policy."
    )


def openai_tools(registry: CapabilityRegistry) -> list[dict[str, Any]]:
    result = []
    for tool in registry.tool_specs("agent"):
        schema = tool.params_model.model_json_schema()
        schema.pop("title", None)
        result.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": schema,
                },
            }
        )
    return result


async def run_turn(
    user_text: str,
    *,
    session_key: str,
    registry: CapabilityRegistry,
    context: dict[str, Any],
) -> str:
    if config.OFFLINE_MODE:
        return (
            f"{config.SPEC.agent.introduction} "
            "The generated solution is running in explicit offline mode; no model or live tool was called."
        )
    lock = _locks.setdefault(session_key, asyncio.Lock())
    async with lock:
        history = list(_histories.get(session_key, []))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_message(registry)},
            *history,
            {"role": "user", "content": user_text},
        ]
        tools = openai_tools(registry)
        final = await _run_loop(messages, tools, registry, context)
        history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": final},
            ]
        )
        _histories[session_key] = history[-_MAX_HISTORY:]
        return final


async def _run_loop(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    registry: CapabilityRegistry,
    context: dict[str, Any],
) -> str:
    for _ in range(config.SPEC.agent.reasoning.max_tool_steps):
        response = await complete(
            model=config.MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.1,
        )
        message = response.choices[0].message
        calls = getattr(message, "tool_calls", None)
        if not calls:
            return message.content or "I do not have a grounded response."
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments or "{}",
                        },
                    }
                    for call in calls
                ],
            }
        )
        for call in calls:
            try:
                arguments = json.loads(call.function.arguments or "{}")
                result = await registry.dispatch_tool(
                    call.function.name, arguments, context=context, surface="agent"
                )
                output = result.model_dump_json()
            except Exception as exc:
                output = json.dumps({"error": f"{type(exc).__name__}: {exc}"})
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": output}
            )
    return "The request exceeded the configured tool-step limit and was stopped safely."
