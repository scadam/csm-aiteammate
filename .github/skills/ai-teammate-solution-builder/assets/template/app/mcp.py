"""Remote MCP streamable-HTTP client with exact per-server authorization."""

from __future__ import annotations

import json
import os
import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from . import agent_identity, config
from .spec import McpServer


class IntegrationUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class McpInvocation:
    data: Any
    provenance: str


class McpInvoker(Protocol):
    async def invoke(
        self,
        server: McpServer,
        tool: str,
        arguments: dict[str, Any],
        offline_result: Any,
        *,
        side_effect: bool = False,
        idempotency_key: str = "",
    ) -> McpInvocation: ...


class ConfiguredMcpInvoker:
    def __init__(self, transport_factory=streamablehttp_client, session_factory=ClientSession):
        self.transport_factory = transport_factory
        self.session_factory = session_factory

    async def invoke(
        self,
        server: McpServer,
        tool: str,
        arguments: dict[str, Any],
        offline_result: Any,
        *,
        side_effect: bool = False,
        idempotency_key: str = "",
    ) -> McpInvocation:
        endpoint = os.getenv(server.endpoint_env, "")
        if config.OFFLINE_MODE:
            if not server.offline:
                raise IntegrationUnavailable(
                    f"MCP server {server.id} does not permit an offline fallback"
                )
            if offline_result is None:
                raise IntegrationUnavailable(
                    f"MCP server {server.id} is offline and has no fallback"
                )
            return McpInvocation(offline_result, f"offline:mcp:{server.id}:{tool}")
        if not endpoint:
            raise IntegrationUnavailable(
                f"MCP endpoint environment variable {server.endpoint_env} is not configured"
            )
        if not endpoint.lower().startswith("https://"):
            raise IntegrationUnavailable(f"MCP server {server.id} requires an HTTPS endpoint")
        headers: dict[str, str] = {}
        if server.auth_mode != "none":
            token = await agent_identity.require_token(
                server.auth_mode, server.scopes, server.token_env
            )
            headers["Authorization"] = f"Bearer {token}"
        result = None
        last_error: Exception | None = None
        attempts = 1 if side_effect else 2
        for attempt in range(attempts):
            try:
                async with self.transport_factory(
                    endpoint, headers=headers, timeout=float(server.timeout_seconds)
                ) as (read_stream, write_stream, _):
                    async with self.session_factory(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=server.timeout_seconds),
                    ) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            tool,
                            arguments,
                            meta=(
                                {"idempotencyKey": idempotency_key}
                                if idempotency_key
                                else None
                            ),
                        )
                break
            except (TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0)
        if result is None:
            raise IntegrationUnavailable(f"MCP server {server.id} failed") from last_error
        if getattr(result, "isError", False):
            raise IntegrationUnavailable(f"MCP tool {server.id}.{tool} returned an error")
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            data: Any = structured
        else:
            blocks = getattr(result, "content", []) or []
            values = [getattr(block, "text", str(block)) for block in blocks]
            text = "\n".join(values)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = text
        return McpInvocation(data, f"live:mcp:{server.id}:{tool}")
