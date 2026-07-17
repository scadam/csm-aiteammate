"""Strict Agent 365 solution specification and reference validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


IdentityMode = Literal[
    "manager_obo", "agentic_user", "managed_identity", "oauth_client_credentials",
    "bearer_env", "native", "none"
]
Exposure = Literal["agent", "workflow", "mcp"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Terms(StrictModel):
    manager_singular: str
    manager_plural: str
    fleet_singular: str
    subject_singular: str
    subject_plural: str
    work_item_singular: str
    work_item_plural: str


class Solution(StrictModel):
    id: str
    name: str
    description: str
    domain: str
    terms: Terms


class HostRuntime(StrictModel):
    framework: str
    port_env: str
    default_port: int


class AgentHostRuntime(HostRuntime):
    framework: Literal["microsoft_365_agents_sdk"]
    transport: Literal["aiohttp"]
    message_path: Literal["/api/messages"]


class McpHostRuntime(HostRuntime):
    framework: Literal["fastmcp"]
    transport: Literal["streamable_http"]


class ControlPlaneRuntime(HostRuntime):
    framework: Literal["fastapi"]


class RuntimeSpec(StrictModel):
    topology: Literal["split", "combined_dev"]
    python_version: str
    agent_host: AgentHostRuntime
    control_plane: ControlPlaneRuntime
    mcp_host: McpHostRuntime


class ReasoningSpec(StrictModel):
    provider: Literal["azure_openai"]
    model_env: str
    default_model: str
    max_tool_steps: int


class AgentSpec(StrictModel):
    display_name: str
    role: str
    instructions: str
    introduction: str
    reasoning: ReasoningSpec


class ServiceConnection(StrictModel):
    name: str


class DelegatedIdentity(StrictModel):
    enabled: Literal[True]
    handler_id: str
    default_scopes: list[str]


class Identity(StrictModel):
    development_mode: bool
    principal_header: str
    manager_header: str
    roles_header: str
    fleet_roles: list[str]
    default_principal_id: str
    default_manager_id: str
    default_roles: list[str]
    fleet_principals: list["FleetPrincipal"]
    service_connection: ServiceConnection
    manager_obo: DelegatedIdentity
    agentic_user: DelegatedIdentity


class Observability(StrictModel):
    provider: Literal["a365"]
    service_name: str
    service_namespace: str
    cluster_category: Literal["dev", "test", "prod"]
    instrument_turns: Literal[True]
    instrument_tools: Literal[True]
    force_flush: Literal[True]


class Manager(StrictModel):
    id: str
    name: str
    principal_id: str
    roles: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class FleetPrincipal(StrictModel):
    principal_id: str
    name: str
    roles: list[str]


class Skill(StrictModel):
    id: str
    title: str
    description: str
    when_to_use: str
    instructions: str
    capabilities: list[str]
    workflows: list[str]


class McpTool(StrictModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class McpServer(StrictModel):
    id: str
    name: str
    endpoint_env: str
    transport: Literal["streamable_http"]
    auth_mode: IdentityMode
    scopes: list[str]
    token_env: str = ""
    offline: bool
    timeout_seconds: int
    tools: list[McpTool]


class AuthSpec(StrictModel):
    mode: IdentityMode
    scopes: list[str]
    token_env: str = ""
    token_url_env: str = ""
    client_id_env: str = ""
    client_secret_env: str = ""


class OpenApiOperation(StrictModel):
    operation_id: str
    capability_id: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    description: str
    input_schema: dict[str, Any]
    side_effect: bool
    expose: list[Exposure]
    offline_result: Any = None


class OpenApiSource(StrictModel):
    id: str
    name: str
    document: str
    base_url_env: str
    allowed_hosts: list[str]
    auth: AuthSpec
    timeout_seconds: int
    operations: list[OpenApiOperation]


class ToolingGateway(StrictModel):
    enabled: Literal[True]
    endpoint_env: str
    registration_id_env: str
    remote_scope_env: str


class McpExposure(StrictModel):
    enabled: Literal[True]
    server_name: str
    tooling_gateway: ToolingGateway


class McpAppsSpec(StrictModel):
    enabled: Literal[True]
    mime_type: Literal["text/html;profile=mcp-app"]
    sandbox: Literal[True]


class AgUiSpec(StrictModel):
    enabled: Literal[True]
    endpoint: Literal["/api/ag-ui"]
    transport: Literal["http_sse"]


class UiSort(StrictModel):
    field: str
    direction: Literal["asc", "desc"]


class UiResource(StrictModel):
    id: str
    title: str
    description: str
    kind: Literal["dashboard", "hitl"]
    tool_name: str
    resource_uri: str
    surfaces: list[Literal["control_plane", "mcp"]]
    audience: Literal["manager", "fleet"]
    source: Literal["workflow_runs", "review_queue"]
    metrics: list[str]
    columns: list[str]
    filters: list[str]
    default_sort: UiSort
    bulk_actions: list[Literal["approve", "reject", "defer"]]
    page_size: int = Field(ge=1, le=200)


class UserInterfaces(StrictModel):
    mcp_apps: McpAppsSpec
    ag_ui: AgUiSpec
    resources: list[UiResource]


class TeamsSsoSpec(StrictModel):
    application_id_source: Literal["a365_blueprint"]
    delegated_scope: Literal["access_agent_as_user"]
    token_transport: Literal["authorization_bearer"]


class TeamsTab(StrictModel):
    id: str
    name: str
    path: Literal["/manager", "/fleet"]
    audience: Literal["manager", "fleet"]


class TeamsAppSpec(StrictModel):
    enabled: Literal[True]
    manifest_version: Literal["1.26"]
    package_name: str
    default_install_scope: Literal["personal"]
    sso: TeamsSsoSpec
    tabs: list[TeamsTab]


class DataSource(StrictModel):
    id: str
    name: str
    kind: Literal["inline", "json"]
    manager_field: str
    subject_id_field: str
    path: str = ""
    records: list[dict[str, Any]] = Field(default_factory=list)


class Idempotency(StrictModel):
    required: bool
    header: str = ""


class Capability(StrictModel):
    id: str
    title: str
    description: str
    kind: Literal[
        "fixture_query", "mcp_tool", "openapi_operation", "rule", "template", "state_action"
    ]
    identity_mode: IdentityMode
    scopes: list[str] = Field(default_factory=list)
    side_effect: bool
    review_mode: Literal["required", "workflow_policy", "none"]
    input_schema: dict[str, Any]
    expose: list[Exposure]
    timeout_seconds: int
    retries: int
    idempotency: Idempotency | None = None
    source: str = ""
    server: str = ""
    tool: str = ""
    openapi_source: str = ""
    operation_id: str = ""
    offline_result: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Condition(StrictModel):
    field: str
    operator: Literal["eq", "ne", "in", "not_in", "exists", "gt", "gte", "lt", "lte"]
    value: Any = None


class ReviewPolicy(StrictModel):
    required_when: list[Condition]
    decisions: list[Literal["approve", "edit", "reject", "defer"]]


class Stage(StrictModel):
    id: str
    title: str
    type: Literal["detect", "enrich", "decide", "generate", "review", "act", "learn"]
    capability: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    on_error: Literal["stop", "continue", "review"]


class WorkflowTestCases(StrictModel):
    review_input: dict[str, Any]
    automatic_input: dict[str, Any]


class Workflow(StrictModel):
    id: str
    title: str
    description: str
    subject_source: str
    trigger_modes: list[Literal["human", "event", "schedule", "agent"]]
    review: ReviewPolicy
    test_cases: WorkflowTestCases | None = None
    stages: list[Stage]


class View(StrictModel):
    title: str
    summary_fields: list[str]


class FleetView(View):
    metrics: list[str]


class ControlPlane(StrictModel):
    manager: View
    fleet: FleetView


class A365Spec(StrictModel):
    cli_managed: Literal[True]
    config_file: Literal["a365.config.json"]
    manifest_dir: Literal["manifest"]
    provisioning_commands: list[str]


class SolutionSpec(StrictModel):
    schema_version: Literal["2.0"]
    solution: Solution
    runtime: RuntimeSpec
    agent: AgentSpec
    identity: Identity
    observability: Observability
    managers: list[Manager]
    skills: list[Skill]
    mcp_servers: list[McpServer]
    openapi_sources: list[OpenApiSource]
    mcp_exposure: McpExposure
    user_interfaces: UserInterfaces
    teams_app: TeamsAppSpec
    data_sources: list[DataSource]
    capabilities: list[Capability]
    workflows: list[Workflow]
    control_plane: ControlPlane
    a365: A365Spec

    @model_validator(mode="after")
    def validate_references(self) -> "SolutionSpec":
        manager_ids = _unique("manager", [item.id for item in self.managers])
        _unique("fleet principal", [item.principal_id for item in self.identity.fleet_principals])
        source_ids = _unique("data source", [item.id for item in self.data_sources])
        capability_ids = _unique("capability", [item.id for item in self.capabilities])
        workflow_ids = _unique("workflow", [item.id for item in self.workflows])
        _unique("skill", [item.id for item in self.skills])
        _unique("MCP server", [item.id for item in self.mcp_servers])
        _unique("OpenAPI source", [item.id for item in self.openapi_sources])
        _unique("UI resource", [item.id for item in self.user_interfaces.resources])
        ui_tool_names = _unique(
            "UI tool", [item.tool_name for item in self.user_interfaces.resources]
        )
        _unique(
            "UI resource URI",
            [item.resource_uri for item in self.user_interfaces.resources],
        )
        reserved_tools = capability_ids | {"get_skill", "resolve_reviews"} | {
            f"start_{workflow_id}" for workflow_id in workflow_ids
        }
        collisions = ui_tool_names & reserved_tools
        if collisions:
            raise ValueError(f"UI tools collide with generated tools: {sorted(collisions)}")
        run_fields = {
            "id", "workflowId", "workflowTitle", "managerId", "subjectId",
            "status", "progress", "currentStage", "createdAt", "updatedAt",
        }
        review_fields = {
            "id", "runId", "workflowId", "workflowTitle", "managerId",
            "subjectId", "capabilityId", "proposedEffect", "status", "digest",
            "createdAt", "updatedAt",
        }
        supported_metrics = {
            "total_runs", "active_runs", "completed_runs", "failed_runs",
            "pending_reviews", "completion_rate", "managers", "subjects",
        }
        for resource in self.user_interfaces.resources:
            if not resource.surfaces:
                raise ValueError(f"UI resource {resource.id} needs at least one surface")
            if "mcp" in resource.surfaces and resource.audience != "manager":
                raise ValueError(
                    f"MCP Apps resource {resource.id} must use the assigned-manager audience"
                )
            if not resource.resource_uri.startswith("ui://"):
                raise ValueError(f"UI resource {resource.id} must use a ui:// URI")
            fields = review_fields if resource.source == "review_queue" else run_fields
            unknown_fields = (set(resource.columns) | set(resource.filters)) - fields
            if unknown_fields or resource.default_sort.field not in fields:
                raise ValueError(
                    f"UI resource {resource.id} references unsupported fields: "
                    f"{sorted(unknown_fields | ({resource.default_sort.field} - fields))}"
                )
            unknown_metrics = set(resource.metrics) - supported_metrics
            if unknown_metrics:
                raise ValueError(
                    f"UI resource {resource.id} references unsupported metrics: "
                    f"{sorted(unknown_metrics)}"
                )
            if resource.kind == "hitl":
                if resource.source != "review_queue" or resource.audience != "manager":
                    raise ValueError(
                        f"HITL resource {resource.id} must be a manager review queue"
                    )
                if not resource.bulk_actions:
                    raise ValueError(f"HITL resource {resource.id} needs bulk actions")
            elif resource.bulk_actions:
                raise ValueError(
                    f"Dashboard resource {resource.id} cannot declare approval actions"
                )
        _unique("Teams tab", [item.id for item in self.teams_app.tabs])
        if not self.teams_app.tabs:
            raise ValueError("teams_app must expose at least one control-plane tab")
        expected_paths = {"manager": "/manager", "fleet": "/fleet"}
        for tab in self.teams_app.tabs:
            if tab.path != expected_paths[tab.audience]:
                raise ValueError(
                    f"Teams tab {tab.id} path does not match its {tab.audience} audience"
                )
        if self.identity.default_manager_id not in manager_ids:
            raise ValueError("identity.default_manager_id must reference a manager")
        for skill in self.skills:
            unknown = set(skill.capabilities) - capability_ids
            if unknown:
                raise ValueError(f"skill {skill.id} references unknown capabilities: {sorted(unknown)}")
            unknown_workflows = set(skill.workflows) - workflow_ids
            if unknown_workflows:
                raise ValueError(f"skill {skill.id} references unknown workflows: {sorted(unknown_workflows)}")
            workflow_by_id = {item.id: item for item in self.workflows}
            for workflow_id in skill.workflows:
                if "agent" not in workflow_by_id[workflow_id].trigger_modes:
                    raise ValueError(
                        f"skill workflow {skill.id}.{workflow_id} must allow agent trigger"
                    )
        servers = {server.id: {tool.name for tool in server.tools} for server in self.mcp_servers}
        server_specs = {server.id: server for server in self.mcp_servers}
        server_tools = {
            server.id: {tool.name: tool for tool in server.tools}
            for server in self.mcp_servers
        }
        source_specs = {source.id: source for source in self.openapi_sources}
        operations = {
            source.id: {operation.operation_id: operation for operation in source.operations}
            for source in self.openapi_sources
        }
        for capability in self.capabilities:
            expected_identity = {
                "fixture_query": "none",
                "rule": "none",
                "template": "managed_identity",
                "state_action": "managed_identity",
            }.get(capability.kind)
            if expected_identity and capability.identity_mode != expected_identity:
                raise ValueError(
                    f"capability {capability.id} kind {capability.kind} must use {expected_identity}"
                )
            if capability.kind == "fixture_query" and capability.source not in source_ids:
                raise ValueError(f"capability {capability.id} references an unknown source")
            if capability.kind == "mcp_tool":
                if capability.server not in servers or capability.tool not in servers[capability.server]:
                    raise ValueError(f"capability {capability.id} references an unknown MCP tool")
                server = server_specs[capability.server]
                tool = server_tools[capability.server][capability.tool]
                if capability.identity_mode != server.auth_mode or capability.scopes != server.scopes:
                    raise ValueError(f"capability {capability.id} disagrees with MCP authorization")
                if capability.input_schema != tool.input_schema:
                    raise ValueError(f"capability {capability.id} disagrees with MCP input schema")
            if capability.kind == "openapi_operation":
                operation = operations.get(capability.openapi_source, {}).get(capability.operation_id)
                if operation is None or operation.capability_id != capability.id:
                    raise ValueError(f"capability {capability.id} references an unknown OpenAPI operation")
                source = source_specs[capability.openapi_source]
                if capability.identity_mode != source.auth.mode or capability.scopes != source.auth.scopes:
                    raise ValueError(f"capability {capability.id} disagrees with OpenAPI authorization")
                if capability.input_schema != operation.input_schema:
                    raise ValueError(f"capability {capability.id} disagrees with OpenAPI input schema")
                if set(capability.expose) != set(operation.expose):
                    raise ValueError(f"capability {capability.id} disagrees with OpenAPI exposure")
            if capability.side_effect:
                if capability.identity_mode == "none":
                    raise ValueError(f"side-effecting capability {capability.id} needs an identity mode")
                if capability.idempotency is None or not capability.idempotency.required:
                    raise ValueError(f"side-effecting capability {capability.id} must require idempotency")
                if capability.review_mode != "none" and ({"agent", "mcp"} & set(capability.expose)):
                    raise ValueError(
                        f"reviewed side effect {capability.id} must be invoked through a workflow"
                    )
            elif capability.review_mode != "none":
                raise ValueError(f"read-only capability {capability.id} cannot require review")
            if capability.identity_mode in {"manager_obo", "agentic_user"} and not capability.scopes:
                raise ValueError(f"capability {capability.id} requires scopes")
            if capability.identity_mode in {"bearer_env", "native"} and capability.kind not in {
                "mcp_tool", "openapi_operation"
            }:
                raise ValueError(
                    f"capability {capability.id} must obtain native/bearer auth from an integration source"
                )
        for source in self.openapi_sources:
            if source.auth.mode == "oauth_client_credentials":
                if not all(
                    [
                        source.auth.token_url_env,
                        source.auth.client_id_env,
                        source.auth.client_secret_env,
                    ]
                ):
                    raise ValueError(
                        f"OpenAPI source {source.id} client credentials is incomplete"
                    )
        for workflow in self.workflows:
            if workflow.subject_source not in source_ids:
                raise ValueError(f"workflow {workflow.id} references an unknown subject source")
            _unique(f"stage in {workflow.id}", [stage.id for stage in workflow.stages])
            for stage in workflow.stages:
                if stage.capability and stage.capability not in capability_ids:
                    raise ValueError(f"stage {workflow.id}.{stage.id} references an unknown capability")
        return self


def _unique(kind: str, values: list[str]) -> set[str]:
    result = set(values)
    if len(result) != len(values):
        raise ValueError(f"duplicate {kind} id")
    return result


def default_spec_path() -> Path:
    configured = os.getenv("SOLUTION_SPEC_PATH")
    if configured:
        return Path(configured)
    project_spec = Path(__file__).resolve().parents[1] / "solution.yaml"
    return project_spec if project_spec.is_file() else Path(__file__).resolve().parent / "solution.yaml"


def load_spec(path: str | Path | None = None) -> SolutionSpec:
    target = Path(path) if path else default_spec_path()
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    return SolutionSpec.model_validate(payload)


def public_spec(spec: SolutionSpec) -> dict[str, Any]:
    return {
        "schemaVersion": spec.schema_version,
        "solution": spec.solution.model_dump(),
        "agent": spec.agent.model_dump(exclude={"instructions"}),
        "runtime": spec.runtime.model_dump(),
        "identity": {
            "managerObo": spec.identity.manager_obo.enabled,
            "agenticUser": spec.identity.agentic_user.enabled,
        },
        "observability": spec.observability.model_dump(),
        "skills": [skill.model_dump(exclude={"instructions"}) for skill in spec.skills],
        "workflows": [workflow.model_dump() for workflow in spec.workflows],
        "capabilities": [
            capability.model_dump(exclude={"offline_result"}) for capability in spec.capabilities
        ],
        "mcpServers": [
            {
                "id": server.id,
                "name": server.name,
                "authMode": server.auth_mode,
                "configured": bool(os.getenv(server.endpoint_env)),
                "offline": server.offline,
                "tools": [tool.model_dump() for tool in server.tools],
            }
            for server in spec.mcp_servers
        ],
        "openapiSources": [
            {
                "id": source.id,
                "name": source.name,
                "configured": bool(os.getenv(source.base_url_env)),
                "operations": [operation.operation_id for operation in source.operations],
            }
            for source in spec.openapi_sources
        ],
        "mcpExposure": spec.mcp_exposure.model_dump(),
        "userInterfaces": spec.user_interfaces.model_dump(),
        "teamsApp": spec.teams_app.model_dump(),
        "controlPlane": spec.control_plane.model_dump(),
        "a365": spec.a365.model_dump(),
    }
