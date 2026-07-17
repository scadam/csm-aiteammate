"""Typed capability registry shared by workflows, reasoning, and FastMCP."""

from __future__ import annotations

import hashlib
import json
import asyncio
import re
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from . import config, observability
from .data import DataCatalog
from .mcp import ConfiguredMcpInvoker, McpInvoker
from .openapi_client import OpenApiInvoker
from .schema_models import model_from_schema
from .skills import SkillCatalog
from .spec import Capability, SolutionSpec
from .state import StateStore, create_state_store
from .user_interfaces import ResolveReviewsInput, UiQuery, UserInterfaceService


class ReviewRequired(PermissionError):
    pass


class GetSkillInput(BaseModel):
    skill_id: str = Field(description="The scenario skill to load before specialist work.")


class WorkflowStartInput(BaseModel):
    subject_id: str = Field(description="The manager-owned subject to process.")
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="Workflow trigger inputs used by deterministic review policy.",
    )


class CapabilityResult(BaseModel):
    capability_id: str
    status: str = "ok"
    data: Any
    provenance: str
    identity_mode: str
    side_effect: bool
    idempotency_key: str = ""
    replayed: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params_model: type[BaseModel]
    capability_id: str
    expose: frozenset[str]
    ui_resource_uri: str = ""
    model_visible: bool = True
    app_visible: bool = False


class CapabilityRegistry:
    def __init__(
        self,
        spec: SolutionSpec,
        data: DataCatalog,
        state: StateStore | None = None,
        mcp: McpInvoker | None = None,
        openapi: OpenApiInvoker | None = None,
    ):
        self.spec = spec
        self.data = data
        self.state = state or create_state_store()
        self.mcp = mcp or ConfiguredMcpInvoker()
        self.openapi = openapi or OpenApiInvoker()
        self.skills = SkillCatalog(spec)
        self.user_interfaces = UserInterfaceService(spec, data, self.state)
        self.capabilities = {item.id: item for item in spec.capabilities}
        self.servers = {item.id: item for item in spec.mcp_servers}
        self.openapi_sources = {item.id: item for item in spec.openapi_sources}
        self.openapi_operations = {
            source.id: {operation.operation_id: operation for operation in source.operations}
            for source in spec.openapi_sources
        }
        self.workflow_engine: Any | None = None
        self._tool_specs = [
            ToolSpec(
                name=item.id,
                description=item.description,
                params_model=model_from_schema(_model_name(item.id), item.input_schema),
                capability_id=item.id,
                expose=frozenset(item.expose),
            )
            for item in spec.capabilities
        ]
        self._tool_specs.append(
            ToolSpec(
                name="get_skill",
                description="Load the full instructions and allowed capabilities of a scenario skill.",
                params_model=GetSkillInput,
                capability_id="__get_skill__",
                expose=frozenset({"agent", "mcp"}),
            )
        )
        for workflow in spec.workflows:
            self._tool_specs.append(
                ToolSpec(
                    name=f"start_{workflow.id}",
                    description=f"Start the policy-governed {workflow.title} workflow.",
                    params_model=WorkflowStartInput,
                    capability_id=f"__workflow__:{workflow.id}",
                    expose=frozenset({"agent", "mcp"}),
                )
            )
        for resource in spec.user_interfaces.resources:
            if "mcp" not in resource.surfaces:
                continue
            self._tool_specs.append(
                ToolSpec(
                    name=resource.tool_name,
                    description=resource.description,
                    params_model=UiQuery,
                    capability_id=f"__ui__:{resource.id}",
                    expose=frozenset({"agent", "mcp"}),
                    ui_resource_uri=resource.resource_uri,
                    app_visible=True,
                )
            )
        if any(
            resource.kind == "hitl" and "mcp" in resource.surfaces
            for resource in spec.user_interfaces.resources
        ):
            self._tool_specs.append(
                ToolSpec(
                    name="resolve_reviews",
                    description="Resolve exact pending review effects selected in a generated HITL MCP App.",
                    params_model=ResolveReviewsInput,
                    capability_id="__ui_resolve_reviews__",
                    expose=frozenset({"mcp"}),
                    model_visible=False,
                    app_visible=True,
                )
            )
        self._by_tool = {item.name: item for item in self._tool_specs}

    def bind_workflow_engine(self, workflow_engine: Any) -> None:
        self.workflow_engine = workflow_engine

    def tool_specs(self, surface: str) -> list[ToolSpec]:
        return [item for item in self._tool_specs if surface in item.expose]

    def validate_arguments(self, capability_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        model = self._by_tool[capability_id].params_model
        return model.model_validate(arguments).model_dump(exclude_none=True)

    @staticmethod
    def review_digest(capability_id: str, inputs: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"capability": capability_id, "inputs": inputs},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    async def dispatch_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        surface: str = "agent",
    ) -> CapabilityResult:
        tool = self._by_tool.get(name)
        if tool is None or surface not in tool.expose:
            raise KeyError(f"Tool {name!r} is not exposed on {surface}")
        validated = tool.params_model.model_validate(arguments).model_dump(exclude_none=True)
        if tool.capability_id == "__get_skill__":
            skill_id = validated["skill_id"]
            return CapabilityResult(
                capability_id="get_skill",
                data=self.skills.instructions(skill_id),
                provenance=f"spec:skill:{skill_id}",
                identity_mode="none",
                side_effect=False,
            )
        if tool.capability_id.startswith("__ui__:"):
            manager_id = _manager_id(context or {})
            if not manager_id:
                raise PermissionError("A verified manager context is required for UI data")
            snapshot = self.user_interfaces.snapshot(
                tool.capability_id.split(":", 1)[1],
                manager_id,
                set((context or {}).get("roles", [])),
                UiQuery.model_validate(validated),
            )
            return CapabilityResult(
                capability_id=tool.name,
                data=snapshot,
                provenance=self.state.provenance,
                identity_mode="none",
                side_effect=False,
            )
        if tool.capability_id == "__ui_resolve_reviews__":
            manager_id = _manager_id(context or {})
            if not manager_id or manager_id != config.AGENT_MANAGER_ID:
                raise PermissionError("Review resolution requires the assigned manager")
            if self.workflow_engine is None:
                raise RuntimeError("Workflow engine is not bound to the capability registry")
            request_key = str((context or {}).get("idempotencyKey", ""))
            if not request_key:
                raise ValueError("A caller idempotency key is required for bulk review")
            decisions = validated["decisions"]
            review_ids = [item["review_id"] for item in decisions]
            if len(review_ids) != len(set(review_ids)):
                raise ValueError("A bulk review request cannot repeat a review ID")
            prepared = []
            for item in decisions:
                review = self.state.get_review(item["review_id"])
                if review is None or review["managerId"] != manager_id:
                    raise PermissionError("Review is outside the assigned manager scope")
                proposed = review.get("context", {}).get("proposedEffect") or {}
                if self.user_interfaces.review_digest(review) != item["expected_digest"]:
                    raise ValueError("Review effect digest is stale or does not match")
                final = item.get("final", {})
                if item["decision"] == "approve" and proposed:
                    final = proposed.get("arguments", {})
                elif item["decision"] == "edit":
                    if not proposed:
                        raise ValueError("An error-recovery review cannot be edited")
                    final = self.validate_arguments(proposed["capabilityId"], final)
                prepared.append((item, final))
            results = []
            for item, final in prepared:
                result = await self.workflow_engine.resolve_review(
                    item["review_id"], manager_id, item["decision"], final
                )
                results.append({"reviewId": item["review_id"], "run": result})
            return CapabilityResult(
                capability_id=tool.name,
                data={"resolved": results},
                provenance="human:review",
                identity_mode="manager_obo",
                side_effect=True,
                idempotency_key=request_key,
            )
        if tool.capability_id.startswith("__workflow__:"):
            if self.workflow_engine is None:
                raise RuntimeError("Workflow engine is not bound to the capability registry")
            manager_id = _manager_id(context or {})
            if not manager_id:
                raise PermissionError("A verified manager context is required to start a workflow")
            if manager_id != config.AGENT_MANAGER_ID:
                raise PermissionError("This Agent ID is assigned to another manager")
            request_key = str((context or {}).get("idempotencyKey", ""))
            if not request_key:
                raise ValueError("A caller idempotency key is required to start a workflow")
            workflow_id = tool.capability_id.split(":", 1)[1]
            run = await self.workflow_engine.start(
                workflow_id,
                manager_id,
                validated["subject_id"],
                "agent",
                validated.get("input", {}),
                request_key=request_key,
            )
            return CapabilityResult(
                capability_id=tool.name,
                data=run,
                provenance="workflow:policy-governed",
                identity_mode="none",
                side_effect=True,
                idempotency_key=request_key,
                replayed=False,
            )
        return await self.execute(tool.capability_id, validated, context=context, surface=surface)

    async def execute(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
        surface: str = "workflow",
    ) -> CapabilityResult:
        capability = self.capabilities[capability_id]
        context = context or {}
        if surface not in capability.expose:
            raise PermissionError(f"Capability {capability_id} is not exposed on {surface}")
        inputs = self.validate_arguments(capability_id, arguments)
        expected_review_digest = self.review_digest(capability_id, inputs)
        if capability.side_effect and capability.review_mode == "required":
            if context.get("approvedEffectDigest") != expected_review_digest:
                raise ReviewRequired(f"Capability {capability_id} requires approval of these exact inputs")
        if capability.side_effect and capability.review_mode == "workflow_policy":
            if not (
                context.get("reviewPolicyCleared", False)
                or context.get("approvedEffectDigest") == expected_review_digest
            ):
                raise ReviewRequired(f"Capability {capability_id} requires policy clearance or exact approval")
        effect_key = self._effect_key(capability, inputs, context) if capability.side_effect else ""
        if effect_key:
            claimed, prior = self.state.claim_effect(effect_key, capability.id)
            if not claimed:
                if prior is None:
                    raise RuntimeError(f"Effect {effect_key} is already in progress")
                return self._result(capability, prior, "replay:idempotent", effect_key, replayed=True)
        conversation_id = str(context.get("conversationId", "")) or None
        scope = None
        try:
            with observability.execute_tool_scope(
                capability.id, _redact(inputs), conversation_id
            ) as scope:
                data, provenance = await self._invoke_with_retry(
                    capability, inputs, context, effect_key
                )
                observability.record_response(scope, _redact(data))
            if effect_key:
                self.state.complete_effect(effect_key, data)
            return self._result(capability, data, provenance, effect_key)
        except Exception as exc:
            observability.record_error(scope, exc)
            if effect_key:
                self.state.fail_effect(effect_key, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            observability.force_flush()

    async def _invoke_with_retry(
        self,
        capability: Capability,
        inputs: dict[str, Any],
        context: dict[str, Any],
        effect_key: str,
    ) -> tuple[Any, str]:
        attempts = 1 + (capability.retries if not capability.side_effect else 0)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._invoke(capability, inputs, context, effect_key)
            except (TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(0)
        assert last_error is not None
        raise last_error

    async def _invoke(
        self,
        capability: Capability,
        inputs: dict[str, Any],
        context: dict[str, Any],
        effect_key: str,
    ) -> tuple[Any, str]:
        if capability.kind == "fixture_query":
            rows = self.data.query(
                capability.source,
                manager_id=_manager_id(context),
                subject_id=context.get("subjectId"),
            )
            return rows, self.data.provenance(capability.source)
        if capability.kind == "mcp_tool":
            invocation = await self.mcp.invoke(
                self.servers[capability.server],
                capability.tool,
                inputs,
                capability.offline_result,
                side_effect=capability.side_effect,
                idempotency_key=effect_key,
            )
            return invocation.data, invocation.provenance
        if capability.kind == "openapi_operation":
            source = self.openapi_sources[capability.openapi_source]
            operation = self.openapi_operations[source.id][capability.operation_id]
            header = capability.idempotency.header if capability.idempotency else ""
            invocation = await self.openapi.invoke(
                source,
                operation,
                inputs,
                capability.offline_result,
                idempotency_key=effect_key,
                idempotency_header=header,
            )
            return invocation.data, invocation.provenance
        if capability.kind == "rule":
            return _clone(capability.offline_result), f"spec:rule:{capability.id}"
        if capability.kind == "template":
            if config.OFFLINE_MODE:
                return _clone(capability.offline_result), f"offline:template:{capability.id}"
            from .model_client import generate_grounded_text

            text = await generate_grounded_text(capability, inputs)
            return text, "live:model:azure_openai"
        if capability.kind == "state_action":
            event = self.state.record_event(
                capability.id,
                _manager_id(context) or "unknown-manager",
                str(context.get("runId") or context.get("idempotencyKey") or ""),
                inputs,
            )
            return event, self.state.provenance
        raise RuntimeError(
            f"Capability {capability.id} requires a generated implementation for kind {capability.kind}"
        )

    @staticmethod
    def _effect_key(
        capability: Capability, inputs: dict[str, Any], context: dict[str, Any]
    ) -> str:
        invocation = context.get("runId") or context.get("idempotencyKey")
        if not invocation:
            raise ValueError(
                f"Side effect {capability.id} requires a workflow run or caller idempotency key"
            )
        value = json.dumps(
            {
                "agent": config.AGENT_ID or config.AGENT_INSTANCE_APP_ID or "development-agent",
                "manager": _manager_id(context) or "unknown-manager",
                "invocation": invocation,
                "capability": capability.id,
                "inputs": inputs,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _result(
        capability: Capability,
        data: Any,
        provenance: str,
        idempotency_key: str,
        *,
        replayed: bool = False,
    ) -> CapabilityResult:
        return CapabilityResult(
            capability_id=capability.id,
            data=data,
            provenance=provenance,
            identity_mode=capability.identity_mode,
            side_effect=capability.side_effect,
            idempotency_key=idempotency_key,
            replayed=replayed,
        )


def _manager_id(context: dict[str, Any]) -> str | None:
    manager = context.get("manager")
    return str(manager.get("id")) if isinstance(manager, dict) and manager.get("id") else None


def _model_name(value: str) -> str:
    return "".join(part.capitalize() for part in value.replace("-", "_").split("_")) + "Input"


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _redact(value: Any) -> Any:
    sensitive = {
        "authorization", "token", "accesstoken", "refreshtoken", "idtoken",
        "apikey", "xapikey", "password", "secret", "clientsecret", "cookie",
        "connectionstring", "sas", "signature", "sig",
    }
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _canonical_key(key) in sensitive else _redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, str):
        lowered = value.lower()
        if (
            lowered.startswith("bearer ")
            or "accountkey=" in lowered
            or "sharedaccesssignature=" in lowered
            or re.fullmatch(
                r"eyj[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+",
                value,
                flags=re.IGNORECASE,
            )
        ):
            return "[REDACTED]"
        if value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment:
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return value


def _canonical_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())
