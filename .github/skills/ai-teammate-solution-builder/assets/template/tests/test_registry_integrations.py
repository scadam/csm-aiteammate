from __future__ import annotations

import json
import contextlib
from types import SimpleNamespace

import httpx
import pytest

from app.capabilities import CapabilityRegistry, ReviewRequired, _redact
from app.mcp import McpInvocation
from app.data import DataCatalog
from app.openapi_client import OpenApiInvoker, OpenApiSecurityError
from app.openapi_client import OpenApiInvocation
from app.mcp import ConfiguredMcpInvoker
from app.spec import load_spec
from app.state import SQLiteStateStore
from app.state import create_state_store
from app.workflows import WorkflowEngine


SPEC = load_spec()
WORKFLOW = SPEC.workflows[0]
MANAGER = next(item for item in SPEC.managers if item.id == SPEC.identity.default_manager_id)
SUBJECT = DataCatalog(SPEC).manager_subjects(MANAGER.id)[0]
REVIEWED_EFFECT = next(
    (item for item in SPEC.capabilities if item.side_effect and item.review_mode != "none"),
    None,
)


def _sample(schema, name="value"):
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type", "string")
    if kind == "object":
        required = set(schema.get("required", []))
        return {
            key: _sample(child, key)
            for key, child in schema.get("properties", {}).items()
            if key in required
        }
    if kind == "array":
        return [_sample(schema.get("items", {}), name)]
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return False
    return f"test-{name}"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    spec = SPEC
    monkeypatch.setattr("app.config.OFFLINE_MODE", True)
    state = SQLiteStateStore(tmp_path / "effects.db")
    data = DataCatalog(spec)
    tools = CapabilityRegistry(spec, data, state)
    tools.bind_workflow_engine(WorkflowEngine(spec, data, state, tools))
    return tools, state


def test_registry_is_typed_and_shared_across_surfaces(registry):
    tools, _ = registry
    agent_names = {tool.name for tool in tools.tool_specs("agent")}
    mcp_names = {tool.name for tool in tools.tool_specs("mcp")}
    assert agent_names == {
        tool.name for tool in tools.tool_specs("mcp") if tool.model_visible
    }
    if "resolve_reviews" in mcp_names:
        assert "resolve_reviews" not in agent_names
    assert "get_skill" in agent_names
    if REVIEWED_EFFECT:
        assert REVIEWED_EFFECT.id not in agent_names
    workflow_tool_name = f"start_{WORKFLOW.id}"
    assert workflow_tool_name in agent_names
    workflow = next(
        tool for tool in tools.tool_specs("agent") if tool.name == workflow_tool_name
    )
    schema = workflow.params_model.model_json_schema()
    assert set(schema["required"]) == {"subject_id"}
    assert schema["properties"]["input"]["type"] == "object"


@pytest.mark.asyncio
async def test_skill_loading_and_review_enforcement(registry):
    tools, state = registry
    skill_spec = SPEC.skills[0]
    skill = await tools.dispatch_tool("get_skill", {"skill_id": skill_spec.id})
    assert skill_spec.instructions in skill.data
    assert f"start_{WORKFLOW.id}" in skill.data
    started = await tools.dispatch_tool(
        f"start_{WORKFLOW.id}",
        {
            "subject_id": SUBJECT["subjectId"],
            "input": WORKFLOW.test_cases.review_input if WORKFLOW.test_cases else {},
        },
        context={"manager": {"id": MANAGER.id}, "idempotencyKey": "caller-1"},
    )
    assert started.data["status"] == "pending_review"
    assert len(state.list_reviews(MANAGER.id, pending_only=True)) == 1


@pytest.mark.asyncio
async def test_side_effect_is_durably_replayed(registry):
    tools, _ = registry
    if REVIEWED_EFFECT is None or REVIEWED_EFFECT.kind != "openapi_operation":
        pytest.skip("Scenario has no reviewed OpenAPI effect")
    arguments = {"incident_id": "inc_1001", "body": {"message": "Grounded update"}}
    context = {
        "runId": "run-1",
        "manager": {"id": MANAGER.id},
        "approvedEffectDigest": tools.review_digest("create_incident_note", arguments),
    }
    first = await tools.execute("create_incident_note", arguments, context=context)
    second = await tools.execute("create_incident_note", arguments, context=context)
    assert first.idempotency_key
    assert second.replayed is True
    assert second.data == first.data


@pytest.mark.asyncio
async def test_review_edit_replaces_the_approved_effect_payload(registry):
    if REVIEWED_EFFECT is None or REVIEWED_EFFECT.kind != "openapi_operation":
        pytest.skip("Scenario has no editable OpenAPI effect")
    tools, state = registry
    captured = []

    class CapturingOpenApi:
        async def invoke(self, source, operation, arguments, offline_result,
                         idempotency_key="", idempotency_header=""):
            captured.append(arguments)
            return OpenApiInvocation({"status": "created"}, "test:openapi")

    tools.openapi = CapturingOpenApi()
    await tools.dispatch_tool(
        f"start_{WORKFLOW.id}",
        {
            "subject_id": SUBJECT["subjectId"],
            "input": WORKFLOW.test_cases.review_input if WORKFLOW.test_cases else {},
        },
        context={"manager": {"id": MANAGER.id}, "idempotencyKey": "edit-run"},
    )
    review = state.list_reviews(MANAGER.id, pending_only=True)[0]
    edited = {
        "incident_id": "inc_1001",
        "body": {"message": "Manager-edited update", "audience": "executives"},
    }
    completed = await tools.workflow_engine.resolve_review(
        review["id"], MANAGER.id, "edit", edited
    )
    note = next(item for item in completed["results"] if item["stageId"] == "note")
    assert note["status"] == "done"
    assert captured == [edited]


@pytest.mark.asyncio
async def test_review_resolution_is_single_winner(registry):
    if REVIEWED_EFFECT is None:
        pytest.skip("Scenario has no reviewed effect")
    tools, state = registry
    await tools.dispatch_tool(
        f"start_{WORKFLOW.id}",
        {
            "subject_id": SUBJECT["subjectId"],
            "input": WORKFLOW.test_cases.review_input if WORKFLOW.test_cases else {},
        },
        context={"manager": {"id": MANAGER.id}, "idempotencyKey": "single-review"},
    )
    review = state.list_reviews(MANAGER.id, pending_only=True)[0]
    approved = review["context"]["proposedEffect"]["arguments"]
    await tools.workflow_engine.resolve_review(
        review["id"], MANAGER.id, "approve", approved
    )
    with pytest.raises(ValueError, match="already been resolved"):
        await tools.workflow_engine.resolve_review(
            review["id"], MANAGER.id, "reject", {}
        )


@pytest.mark.asyncio
async def test_openapi_request_binding_and_idempotency(monkeypatch):
    spec = SPEC
    if not spec.openapi_sources:
        pytest.skip("Scenario has no OpenAPI source")
    source = spec.openapi_sources[0]
    operation = source.operations[0]
    arguments = _sample(operation.input_schema)
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            201,
            headers={"content-type": "application/json"},
            json={"status": "created", "note_id": "note-1"},
        )

    monkeypatch.setattr("app.config.OFFLINE_MODE", False)
    host = source.allowed_hosts[0]
    monkeypatch.setenv(source.base_url_env, f"https://{host}")
    invoker = OpenApiInvoker(httpx.MockTransport(handler))
    if source.auth.mode == "oauth_client_credentials":
        monkeypatch.setenv(source.auth.token_url_env, f"https://{host}/services/oauth2/token")
        monkeypatch.setenv(source.auth.client_id_env, "client-id")
        monkeypatch.setenv(source.auth.client_secret_env, "client-secret")

        async def oauth_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/services/oauth2/token"):
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    json={"access_token": "oauth-token", "instance_url": f"https://{host}"},
                )
            return await handler(request)

        invoker = OpenApiInvoker(httpx.MockTransport(oauth_handler))
    else:
        async def fake_token(mode, scopes, token_env=""):
            assert mode == source.auth.mode
            return "agent-token"

        monkeypatch.setattr("app.openapi_client.agent_identity.require_token", fake_token)
    result = await invoker.invoke(
        source,
        operation,
        arguments,
        operation.offline_result,
        idempotency_key="effect-key",
        idempotency_header="Idempotency-Key",
    )
    request = captured["request"]
    assert request.url.path
    assert request.headers["authorization"].startswith("Bearer ")
    if operation.side_effect:
        assert request.headers.get("idempotency-key") == "effect-key"
    assert result.provenance == f"live:openapi:{source.id}:{operation.operation_id}"


def test_openapi_rejects_unapproved_host(monkeypatch):
    spec = SPEC
    if not spec.openapi_sources:
        pytest.skip("Scenario has no OpenAPI source")
    source = spec.openapi_sources[0]
    monkeypatch.setenv(source.base_url_env, "https://metadata.internal")
    with pytest.raises(OpenApiSecurityError, match="allowlisted"):
        OpenApiInvoker()._validate_base_url("https://metadata.internal", source.allowed_hosts)


def test_production_state_store_fails_closed_without_shared_endpoint(monkeypatch):
    monkeypatch.setattr("app.config.DEVELOPMENT_MODE", False)
    monkeypatch.setattr("app.config.STATE_TABLE_ENDPOINT", "")
    with pytest.raises(RuntimeError, match="STATE_TABLE_ENDPOINT"):
        create_state_store()


@pytest.mark.asyncio
async def test_live_remote_mcp_uses_manager_obo(monkeypatch):
    spec = SPEC
    server = spec.mcp_servers[0]
    captured = {}

    @contextlib.asynccontextmanager
    async def transport(url, headers=None, timeout=30):
        captured.update({"url": url, "headers": headers, "timeout": timeout})
        yield object(), object(), lambda: None

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments, meta=None):
            captured.update({"tool": name, "arguments": arguments, "meta": meta})
            return SimpleNamespace(
                isError=False,
                structuredContent={"answer": "live collaboration context"},
                content=[],
            )

    async def token(mode, scopes, token_env=""):
        assert mode == "manager_obo"
        assert scopes == server.scopes
        return "manager-obo-token"

    monkeypatch.setattr("app.config.OFFLINE_MODE", False)
    monkeypatch.setenv(server.endpoint_env, "https://workiq.example.test/mcp")
    monkeypatch.setattr("app.mcp.agent_identity.require_token", token)
    invocation = await ConfiguredMcpInvoker(transport, Session).invoke(
        server,
        "ask",
        {"question": "What changed?"},
        {"answer": "offline"},
    )
    assert captured["headers"] == {"Authorization": "Bearer manager-obo-token"}
    assert captured["tool"] == "ask"
    assert invocation.data == {"answer": "live collaboration context"}
    assert invocation.provenance == "live:mcp:workiq:ask"


@pytest.mark.asyncio
async def test_declared_read_retry_executes(tmp_path, monkeypatch):
    spec = load_spec()
    data = DataCatalog(spec)
    state = SQLiteStateStore(tmp_path / "retry.db")

    class FlakyMcp:
        def __init__(self):
            self.calls = 0

        async def invoke(
            self,
            server,
            tool,
            arguments,
            offline_result,
            *,
            side_effect=False,
            idempotency_key="",
        ):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("transient")
            return McpInvocation({"answer": "recovered"}, "live:mcp:retry")

    capability = next(
        (item for item in spec.capabilities if item.kind == "mcp_tool" and not item.side_effect),
        None,
    )
    if capability is None:
        pytest.skip("Scenario has no read-only MCP capability")
    flaky = FlakyMcp()
    tools = CapabilityRegistry(spec, data, state, flaky)
    result = await tools.execute(
        capability.id,
        _sample(capability.input_schema),
        context={"manager": {"id": MANAGER.id}},
    )
    assert flaky.calls == 2
    assert result.data == {"answer": "recovered"}


def test_telemetry_redaction_is_recursive():
    redacted = _redact(
        {
            "question": "safe",
            "accessToken": "secret",
            "nested": {"x-api-key": "secret"},
            "auth": "Bearer abc",
            "jwt": "eyJabc.eyJdef.signature",
            "storage": "DefaultEndpointsProtocol=https;AccountKey=secret",
            "url": "https://example.test/path?sig=secret&x=1",
        }
    )
    assert redacted == {
        "question": "safe",
        "accessToken": "[REDACTED]",
        "nested": {"x-api-key": "[REDACTED]"},
        "auth": "[REDACTED]",
        "jwt": "[REDACTED]",
        "storage": "[REDACTED]",
        "url": "https://example.test/path",
    }
