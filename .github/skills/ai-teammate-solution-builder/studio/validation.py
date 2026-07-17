"""Shared schema and semantic validation for Studio and scaffold execution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


DEFAULT_SCHEMA = Path(__file__).resolve().parents[1] / "assets" / "solution.schema.json"


def load_spec(path: str | Path) -> dict[str, Any]:
    target = Path(path).resolve()
    text = target.read_text(encoding="utf-8")
    value = json.loads(text) if target.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("The solution specification must be an object.")
    return value


def validate_spec(
    spec: dict[str, Any],
    spec_path: str | Path | None = None,
    *,
    schema_path: str | Path = DEFAULT_SCHEMA,
) -> None:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(spec), key=lambda item: list(item.path))
    if errors:
        detail = "\n".join(
            f"- {'.'.join(map(str, error.path)) or '<root>'}: {error.message}"
            for error in errors
        )
        raise ValueError(f"Specification validation failed:\n{detail}")

    source_ids = {item["id"] for item in spec["data_sources"]}
    manager_ids = {item["id"] for item in spec["managers"]}
    capability_ids = {item["id"] for item in spec["capabilities"]}
    workflow_ids = {item["id"] for item in spec["workflows"]}
    server_tools = {
        item["id"]: {tool["name"] for tool in item["tools"]} for item in spec["mcp_servers"]
    }
    servers = {item["id"]: item for item in spec["mcp_servers"]}
    server_tool_specs = {
        item["id"]: {tool["name"]: tool for tool in item["tools"]}
        for item in spec["mcp_servers"]
    }
    openapi_sources = {item["id"]: item for item in spec["openapi_sources"]}
    openapi_operations = {
        source["id"]: {operation["operation_id"]: operation for operation in source["operations"]}
        for source in spec["openapi_sources"]
    }
    if spec["identity"]["default_manager_id"] not in manager_ids:
        raise ValueError("identity.default_manager_id does not reference a declared manager.")
    _require_unique("manager", [item["id"] for item in spec["managers"]])
    _require_unique("skill", [item["id"] for item in spec["skills"]])
    _require_unique("MCP server", [item["id"] for item in spec["mcp_servers"]])
    _require_unique("OpenAPI source", [item["id"] for item in spec["openapi_sources"]])
    _require_unique("data source", [item["id"] for item in spec["data_sources"]])
    _require_unique("capability", [item["id"] for item in spec["capabilities"]])
    _require_unique("workflow", [item["id"] for item in spec["workflows"]])

    workflow_by_id = {item["id"]: item for item in spec["workflows"]}
    for skill in spec["skills"]:
        unknown = set(skill["capabilities"]) - capability_ids
        if unknown:
            raise ValueError(f"Skill {skill['id']} references unknown capabilities: {sorted(unknown)}")
        unknown_workflows = set(skill["workflows"]) - workflow_ids
        if unknown_workflows:
            raise ValueError(
                f"Skill {skill['id']} references unknown workflows: {sorted(unknown_workflows)}"
            )
        for workflow_id in skill["workflows"]:
            if "agent" not in workflow_by_id[workflow_id]["trigger_modes"]:
                raise ValueError(
                    f"Skill workflow {skill['id']}.{workflow_id} must allow the agent trigger."
                )

    for capability in spec["capabilities"]:
        _reject_credential_fields(capability["id"], capability["input_schema"])
        expected_identity = {
            "fixture_query": "none",
            "rule": "none",
            "template": "managed_identity",
            "state_action": "managed_identity",
        }.get(capability["kind"])
        if expected_identity and capability["identity_mode"] != expected_identity:
            raise ValueError(
                f"Capability {capability['id']} kind {capability['kind']} must use {expected_identity}."
            )
        if capability["kind"] == "fixture_query" and capability.get("source") not in source_ids:
            raise ValueError(f"Capability {capability['id']} references an unknown data source.")
        if capability["kind"] == "mcp_tool":
            server = capability.get("server")
            if server not in server_tools or capability.get("tool") not in server_tools[server]:
                raise ValueError(f"Capability {capability['id']} references an unknown MCP tool.")
            source = servers[server]
            tool_spec = server_tool_specs[server][capability["tool"]]
            if capability["identity_mode"] != source["auth_mode"]:
                raise ValueError(f"Capability {capability['id']} disagrees with MCP auth mode.")
            if capability.get("scopes", []) != source.get("scopes", []):
                raise ValueError(f"Capability {capability['id']} disagrees with MCP scopes.")
            if capability["input_schema"] != tool_spec["input_schema"]:
                raise ValueError(f"Capability {capability['id']} disagrees with MCP input schema.")
        if capability["kind"] == "openapi_operation":
            source = capability.get("openapi_source")
            operation_id = capability.get("operation_id")
            operation = openapi_operations.get(source, {}).get(operation_id)
            if operation is None or operation["capability_id"] != capability["id"]:
                raise ValueError(
                    f"Capability {capability['id']} references an unknown OpenAPI operation."
                )
            if operation["side_effect"] != capability["side_effect"]:
                raise ValueError(
                    f"Capability {capability['id']} disagrees with its OpenAPI side-effect policy."
                )
            source_spec = openapi_sources[source]
            if capability["identity_mode"] != source_spec["auth"]["mode"]:
                raise ValueError(f"Capability {capability['id']} disagrees with OpenAPI auth mode.")
            if capability.get("scopes", []) != source_spec["auth"].get("scopes", []):
                raise ValueError(f"Capability {capability['id']} disagrees with OpenAPI scopes.")
            if capability["input_schema"] != operation["input_schema"]:
                raise ValueError(f"Capability {capability['id']} disagrees with OpenAPI input schema.")
            if set(capability["expose"]) != set(operation["expose"]):
                raise ValueError(f"Capability {capability['id']} disagrees with OpenAPI exposure.")
        if capability["side_effect"] and capability["identity_mode"] == "none":
            raise ValueError(
                f"Side-effecting capability {capability['id']} must declare an identity mode."
            )
        if capability["side_effect"] and not capability.get("idempotency", {}).get("required"):
            raise ValueError(
                f"Side-effecting capability {capability['id']} must require idempotency."
            )
        if not capability["side_effect"] and capability["review_mode"] != "none":
            raise ValueError(f"Read-only capability {capability['id']} cannot require review.")
        if capability["side_effect"] and capability["review_mode"] != "none":
            if {"agent", "mcp"}.intersection(capability["expose"]):
                raise ValueError(
                    f"Reviewed side effect {capability['id']} must be invoked through a workflow."
                )
        if capability["identity_mode"] in {"manager_obo", "agentic_user"} and not capability.get(
            "scopes"
        ):
            raise ValueError(
                f"{capability['identity_mode'].replace('_', '-').title()} capability "
                f"{capability['id']} must declare scopes."
            )
        if capability["identity_mode"] in {"bearer_env", "native"}:
            raise ValueError(
                f"Capability {capability['id']} must obtain native/bearer auth from its MCP or "
                "OpenAPI source."
            )

    for source in spec["openapi_sources"]:
        auth = source["auth"]
        if auth["mode"] == "oauth_client_credentials":
            required = ["token_url_env", "client_id_env", "client_secret_env"]
            missing = [name for name in required if not auth.get(name)]
            if missing:
                raise ValueError(
                    f"OpenAPI source {source['id']} client credentials is missing: {missing}"
                )

    capabilities = {item["id"]: item for item in spec["capabilities"]}
    for workflow in spec["workflows"]:
        if workflow["subject_source"] not in source_ids:
            raise ValueError(f"Workflow {workflow['id']} references an unknown subject source.")
        review_seen = False
        for stage in workflow["stages"]:
            if stage["type"] == "review":
                review_seen = True
            capability = stage.get("capability")
            if capability and capability not in capability_ids:
                raise ValueError(
                    f"Stage {workflow['id']}.{stage['id']} references an unknown capability."
                )
            if (
                capability
                and capabilities[capability]["review_mode"] in {"required", "workflow_policy"}
                and not review_seen
            ):
                raise ValueError(
                    f"Side effect {workflow['id']}.{stage['id']} must follow a review stage."
                )

    if spec_path is not None:
        target = Path(spec_path).resolve()
        _validate_openapi_documents(spec, target)
        _validate_json_sources(spec, target)


def confined_source(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise ValueError(f"Source path must be relative and contained: {relative}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Source path escapes the specification root: {relative}")
    return resolved


def _require_unique(kind: str, values: list[str]) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {kind} id.")


def _reject_credential_fields(capability_id: str, schema: dict[str, Any]) -> None:
    forbidden = {
        "authorization",
        "token",
        "access_token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "client_secret",
        "cookie",
        "host",
        "url",
        "method",
    }
    for name, child in schema.get("properties", {}).items():
        normalized = name.lower().replace("-", "_")
        if normalized in forbidden:
            raise ValueError(
                f"Capability {capability_id} exposes forbidden credential/transport field {name!r}."
            )
        if isinstance(child, dict) and child.get("type") == "object":
            _reject_credential_fields(capability_id, child)


def _validate_openapi_documents(spec: dict[str, Any], spec_path: Path) -> None:
    for source in spec["openapi_sources"]:
        document_path = confined_source(spec_path.parent, source["document"])
        if not document_path.is_file():
            raise ValueError(f"OpenAPI source {source['id']} does not exist: {document_path}")
        document = load_spec(document_path)
        if not str(document.get("openapi", "")).startswith("3."):
            raise ValueError(f"OpenAPI source {source['id']} must use OpenAPI 3.x.")
        paths = document.get("paths", {})
        for operation in source["operations"]:
            path_item = paths.get(operation["path"], {})
            actual = path_item.get(operation["method"].lower())
            if not isinstance(actual, dict) or actual.get("operationId") != operation["operation_id"]:
                raise ValueError(
                    f"OpenAPI operation {source['id']}.{operation['operation_id']} does not match "
                    "the declared method/path."
                )
            _validate_openapi_input_schema(source["id"], operation, path_item, actual)


def _validate_json_sources(spec: dict[str, Any], spec_path: Path) -> None:
    for source in spec["data_sources"]:
        if source["kind"] != "json":
            continue
        source_path = confined_source(spec_path.parent, source["path"])
        if not source_path.is_file():
            raise ValueError(f"JSON data source {source['id']} does not exist: {source_path}")
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"JSON data source {source['id']} is invalid: {exc}") from exc
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError(f"JSON data source {source['id']} must contain an array of objects.")


def _validate_openapi_input_schema(
    source_id: str,
    operation: dict[str, Any],
    path_item: dict[str, Any],
    actual: dict[str, Any],
) -> None:
    declared = operation["input_schema"]
    properties = declared.get("properties", {})
    declared_required = set(declared.get("required", []))
    parameters = [*path_item.get("parameters", []), *actual.get("parameters", [])]
    expected: dict[str, tuple[str, bool, dict[str, Any]]] = {}
    for parameter in parameters:
        if "$ref" in parameter:
            raise ValueError(
                f"OpenAPI operation {source_id}.{operation['operation_id']} uses an unsupported "
                "parameter $ref."
            )
        location = parameter.get("in")
        if location not in {"path", "query"}:
            raise ValueError(
                f"OpenAPI operation {source_id}.{operation['operation_id']} uses unsupported "
                f"parameter location {location!r}."
            )
        expected[parameter["name"]] = (
            location,
            bool(parameter.get("required")),
            parameter.get("schema", {}),
        )
    request_body = actual.get("requestBody")
    if request_body:
        content = request_body.get("content", {})
        body_schema = content.get("application/json", {}).get("schema")
        if not isinstance(body_schema, dict):
            raise ValueError(
                f"OpenAPI operation {source_id}.{operation['operation_id']} must use an inline "
                "application/json body."
            )
        expected["body"] = ("body", bool(request_body.get("required")), body_schema)
    if set(properties) != set(expected):
        raise ValueError(
            f"OpenAPI operation {source_id}.{operation['operation_id']} input properties must be "
            f"exactly {sorted(expected)}."
        )
    for name, (location, required, source_schema) in expected.items():
        declared_schema = properties[name]
        if declared_schema.get("x-in", "query") != location:
            raise ValueError(
                f"OpenAPI input {source_id}.{operation['operation_id']}.{name} has the wrong x-in "
                "location."
            )
        if required != (name in declared_required):
            raise ValueError(
                f"OpenAPI input {source_id}.{operation['operation_id']}.{name} has the wrong "
                "required policy."
            )
        _compare_schema_shape(
            f"{source_id}.{operation['operation_id']}.{name}", declared_schema, source_schema
        )


def _compare_schema_shape(name: str, declared: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("type", "format", "enum"):
        if key in source and declared.get(key) != source.get(key):
            raise ValueError(f"OpenAPI input {name} disagrees on {key}.")
    if source.get("type") == "object":
        if set(declared.get("properties", {})) != set(source.get("properties", {})):
            raise ValueError(f"OpenAPI input {name} has different object properties.")
        if set(declared.get("required", [])) != set(source.get("required", [])):
            raise ValueError(f"OpenAPI input {name} has different required properties.")
