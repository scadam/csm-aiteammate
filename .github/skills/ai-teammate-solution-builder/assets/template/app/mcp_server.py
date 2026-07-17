"""FastMCP facade generated from the same typed capability registry as the agent."""

from __future__ import annotations

import inspect
import logging
import jwt

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic_core import PydanticUndefined

from . import agent_identity, config, observability
from .capabilities import CapabilityRegistry, ToolSpec
from .data import DataCatalog
from .mcp_auth import build_token_verifier
from .state import create_state_store
from .user_interfaces import mcp_app_html
from .workflows import WorkflowEngine


logger = logging.getLogger(__name__)


def _signature(tool: ToolSpec) -> tuple[inspect.Signature, dict[str, object]]:
    parameters: list[inspect.Parameter] = []
    annotations: dict[str, object] = {}
    for name, field in tool.params_model.model_fields.items():
        annotation = field.annotation or str
        default = inspect.Parameter.empty if field.is_required() else field.default
        if default is PydanticUndefined:
            default = inspect.Parameter.empty
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )
        annotations[name] = annotation
    if tool.capability_id.startswith("__workflow__:") or tool.capability_id == "__ui_resolve_reviews__":
        side_effect = True
    elif tool.capability_id == "__get_skill__" or tool.capability_id.startswith("__ui__:"):
        side_effect = False
    else:
        capability = next(
            item for item in config.SPEC.capabilities if item.id == tool.capability_id
        )
        side_effect = capability.side_effect
    if side_effect:
            parameters.append(
                inspect.Parameter(
                    "idempotency_key",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=str,
                )
            )
            annotations["idempotency_key"] = str
    annotations["return"] = str
    return inspect.Signature(parameters), annotations


def _callable(tool: ToolSpec, registry: CapabilityRegistry):
    async def run(**kwargs):
        idempotency_key = kwargs.pop("idempotency_key", "")
        access = get_access_token()
        assertion = access.token if access else ""
        principal_id = ""
        if assertion:
            claims = jwt.decode(
                assertion,
                options={
                    "verify_signature": False,
                    "verify_aud": False,
                    "verify_exp": False,
                },
            )
            principal_id = str(claims.get("oid") or claims.get("sub") or "")
        manager = next(
            (item for item in config.SPEC.managers if item.principal_id == principal_id),
            None,
        )
        if not config.MCP_ALLOW_DEV_NO_AUTH and manager is None:
            raise PermissionError("The Tooling Gateway principal has no manager assignment")
        if manager is not None and manager.id != config.AGENT_MANAGER_ID:
            raise PermissionError("This Agent ID is assigned to another manager")
        request_context = agent_identity.request_context(
            manager.id if manager else config.AGENT_MANAGER_ID,
            "mcp",
            principal_id=principal_id,
            inbound_assertion=assertion,
        )
        token = agent_identity.set_context(request_context)
        try:
            result = await registry.dispatch_tool(
                tool.name,
                kwargs,
                context={
                    "manager": {"id": request_context.manager_id},
                    "roles": list(manager.roles) if manager else [],
                    "idempotencyKey": idempotency_key,
                },
                surface="mcp",
            )
            return result.model_dump_json()
        finally:
            agent_identity.reset_context(token)

    signature, annotations = _signature(tool)
    run.__name__ = tool.name
    run.__doc__ = tool.description
    run.__signature__ = signature  # type: ignore[attr-defined]
    run.__annotations__ = annotations
    return run


def _resource_callable(resource):
    def render_ui() -> str:
        return mcp_app_html(resource)

    render_ui.__name__ = f"render_{resource.id}"
    return render_ui


def build_server(registry: CapabilityRegistry | None = None) -> FastMCP:
    if registry is None:
        state = create_state_store()
        data = DataCatalog(config.SPEC)
        registry = CapabilityRegistry(config.SPEC, data, state)
        registry.bind_workflow_engine(
            WorkflowEngine(config.SPEC, data, state, registry)
        )
    verifier = build_token_verifier()
    auth = None
    if verifier is not None:
        if not all(
            [
                config.MCP_TOKEN_ISSUER,
                config.MCP_TOKEN_AUDIENCE,
                config.MCP_RESOURCE_SERVER_URL,
            ]
        ):
            raise RuntimeError("Production MCP authentication is not configured")
        auth = AuthSettings(
            issuer_url=config.MCP_TOKEN_ISSUER,
            resource_server_url=config.MCP_RESOURCE_SERVER_URL,
            required_scopes=[config.MCP_REQUIRED_SCOPE],
        )
    server = FastMCP(
        name=config.SPEC.mcp_exposure.server_name,
        instructions=(
            f"Governed capabilities for {config.SPEC.agent.display_name}. "
            "Use the Agent 365 Tooling Gateway in production."
        ),
        host=config.MCP_HOST,
        port=config.MCP_PORT,
        token_verifier=verifier,
        auth=auth,
    )
    for tool in registry.tool_specs("mcp"):
        visibility = []
        if tool.model_visible:
            visibility.append("model")
        if tool.app_visible:
            visibility.append("app")
        ui_meta: dict[str, object] = {"visibility": visibility}
        if tool.ui_resource_uri:
            ui_meta["resourceUri"] = tool.ui_resource_uri
        server.add_tool(
            _callable(tool, registry),
            name=tool.name,
            description=tool.description,
            meta={"ui": ui_meta},
        )
    for resource in config.SPEC.user_interfaces.resources:
        if "mcp" not in resource.surfaces:
            continue
        server.resource(
            resource.resource_uri,
            name=resource.id,
            title=resource.title,
            description=resource.description,
            mime_type=config.SPEC.user_interfaces.mcp_apps.mime_type,
        )(_resource_callable(resource))
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    observability.configure_a365()
    import asyncio

    asyncio.run(observability.setup_standalone_export_token())
    try:
        build_server().run(transport="streamable-http")
    finally:
        observability.force_flush()


if __name__ == "__main__":
    main()
