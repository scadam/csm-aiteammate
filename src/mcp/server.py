"""
MCP server exposing this agent's capabilities as MCP tools.

Each capability is registered from the single source of truth in
``src.tools.TOOL_SPECS`` (the same specs that back the Copilot tools), so the
MCP surface and the reasoning-loop surface never drift. Tool input schemas are
generated from each capability's Pydantic parameter model.

The server is the governed entry point the A365 Tooling Gateway registers (see
``gateway.py``); run it standalone with ``python -m src.mcp.server``.
"""

from __future__ import annotations

import inspect
import logging
from functools import partial

from mcp.server.fastmcp import FastMCP
from pydantic_core import PydanticUndefined

from .. import config
from ..tools import TOOL_SPECS, ToolSpec

logger = logging.getLogger(__name__)


def _build_signature(spec: ToolSpec) -> inspect.Signature:
    """Build an inspect.Signature from a ToolSpec's Pydantic parameter model."""
    params: list[inspect.Parameter] = []
    annotations: dict = {}
    for name, field in spec.params_model.model_fields.items():
        annotation = field.annotation if field.annotation is not None else str
        default = inspect.Parameter.empty if field.is_required() else field.default
        if default is PydanticUndefined:
            default = inspect.Parameter.empty
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )
        annotations[name] = annotation
    annotations["return"] = str
    return inspect.Signature(params), annotations


def _make_mcp_callable(spec: ToolSpec):
    """Wrap a capability coroutine with a synthetic typed signature for FastMCP."""

    async def _runner(**kwargs):
        result = await spec.func(**kwargs)
        # Log the MCP tool CALL to Purview DSPM (real processContent "Tool call"
        # event) so invocations through the A365 Tooling Gateway are visible in DSPM,
        # attributed to this agent. Best-effort: never blocks or alters the result.
        try:
            from .. import purview, identity
            mgr = identity.resolve_manager() or {}
            await purview.log_tool_call(
                tool=spec.name, surface="MCP tool",
                manager={"id": mgr.get("manager_id"), "name": mgr.get("display_name"),
                         "entra_object_id": mgr.get("entra_object_id")},
                arguments=kwargs, result=result if isinstance(result, str) else str(result))
        except Exception:  # pragma: no cover - logging must never break the tool
            logger.debug("MCP tool-call DSPM logging skipped", exc_info=True)
        return result

    signature, annotations = _build_signature(spec)
    _runner.__name__ = spec.name
    _runner.__doc__ = spec.description
    _runner.__signature__ = signature  # type: ignore[attr-defined]
    _runner.__annotations__ = annotations
    return _runner


def build_server() -> FastMCP:
    server = FastMCP(
        name="csm-ai-teammate",
        instructions="Capabilities of the Digital CSM AI Teammate.",
        host=config.MCP_HOST,
        port=config.MCP_PORT,
    )
    for spec in TOOL_SPECS:
        server.add_tool(
            _make_mcp_callable(spec),
            name=spec.name,
            description=spec.description,
        )
    logger.info("MCP server built with %d tools.", len(TOOL_SPECS))
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
