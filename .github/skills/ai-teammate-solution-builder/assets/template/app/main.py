"""FastAPI application generated from solution.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from ag_ui.core import RunAgentInput
from pydantic import BaseModel, Field

from .capabilities import CapabilityRegistry
from .ag_ui import AgUiAdapter
from . import agent_identity
from . import observability
from .data import DataCatalog
from .identity import (
    HeaderIdentityProvider,
    IdentityProvider,
    Principal,
    manager_for,
    require_fleet,
    require_manager,
    require_manager_read,
)
from .mcp import McpInvoker
from .readiness import snapshot as readiness_snapshot
from .spec import SolutionSpec, load_spec, public_spec
from .state import StateStore, create_state_store
from .ui import control_plane_page
from .user_interfaces import UiQuery, UserInterfaceService
from .workflows import WorkflowEngine


class RunRequest(BaseModel):
    subject_id: str
    manager_id: str | None = None
    trigger_mode: str = "human"
    input: dict[str, Any] = Field(default_factory=dict)


class ReviewRequest(BaseModel):
    decision: str
    final: dict[str, Any] = Field(default_factory=dict)


class Runtime:
    def __init__(
        self,
        spec: SolutionSpec,
        state: StateStore,
        identity: IdentityProvider,
        data: DataCatalog,
        capabilities: CapabilityRegistry,
        workflows: WorkflowEngine,
    ):
        self.spec = spec
        self.state = state
        self.identity = identity
        self.data = data
        self.capabilities = capabilities
        self.workflows = workflows
        self.user_interfaces = UserInterfaceService(spec, data, state)
        self.ag_ui = AgUiAdapter(self.user_interfaces, workflows)


def create_app(
    *,
    spec_path: str | Path | None = None,
    state_path: str | Path | None = None,
    identity_provider: IdentityProvider | None = None,
    mcp_invoker: McpInvoker | None = None,
) -> FastAPI:
    spec = load_spec(spec_path)
    state_store = create_state_store(state_path)
    data = DataCatalog(spec)
    identity = identity_provider or HeaderIdentityProvider(spec)
    capabilities = CapabilityRegistry(spec, data, state_store, mcp_invoker)
    workflows = WorkflowEngine(spec, data, state_store, capabilities)
    capabilities.bind_workflow_engine(workflows)
    runtime = Runtime(spec, state_store, identity, data, capabilities, workflows)
    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        observability.configure_a365()
        await observability.setup_standalone_export_token()
        try:
            yield
        finally:
            observability.force_flush()

    application = FastAPI(
        title=spec.solution.name,
        description=spec.solution.description,
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.runtime = runtime

    frame_ancestors = " ".join(
        [
            "'self'",
            "https://teams.microsoft.com",
            "https://*.teams.microsoft.com",
            "https://teams.cloud.microsoft",
            "https://*.teams.cloud.microsoft",
            "https://*.cloud.microsoft",
            "https://outlook.office.com",
            "https://outlook.office365.com",
            "https://*.office.com",
            "https://microsoft365.com",
            "https://*.microsoft365.com",
            "https://*.sharepoint.com",
        ]
    )

    @application.middleware("http")
    async def teams_host_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = f"frame-ancestors {frame_ancestors};"
        if "x-frame-options" in response.headers:
            del response.headers["x-frame-options"]
        content_type = response.headers.get("content-type", "").lower()
        if (
            request.url.path in {"/", "/manager", "/fleet"}
            or request.url.path.startswith("/api/")
            or "text/html" in content_type
            or "javascript" in content_type
            or "application/json" in content_type
        ):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            for header in ("etag", "last-modified"):
                if header in response.headers:
                    del response.headers[header]
        return response

    async def principal(request: Request) -> Principal:
        return await runtime.identity.resolve(request)

    @application.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/manager")

    @application.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
    async def privacy() -> str:
        return "<h1>Privacy</h1><p>This AI teammate processes only the data and scopes declared in its solution specification.</p>"

    @application.get("/terms", response_class=HTMLResponse, include_in_schema=False)
    async def terms() -> str:
        return "<h1>Terms of use</h1><p>Use is subject to your organization's Agent 365 governance and review policies.</p>"

    @application.get("/health")
    async def health() -> dict[str, Any]:
        readiness = readiness_snapshot()
        return {
            "status": readiness["status"],
            "solution": spec.solution.id,
            "schemaVersion": spec.schema_version,
            "stateStore": (
                "sqlite-development"
                if agent_identity.config.DEVELOPMENT_MODE
                else "azure-table-managed-identity"
            ),
            "liveReady": readiness["liveReady"],
            "checks": readiness["checks"],
        }

    @application.get("/api/spec")
    async def get_spec() -> dict[str, Any]:
        return public_spec(spec)

    @application.get("/api/me")
    async def get_me(current: Principal = Depends(principal)) -> dict[str, Any]:
        manager = manager_for(spec, current.manager_id) if current.manager_id else None
        return {
            "principalId": current.principal_id,
            "managerId": current.manager_id,
            "managerName": manager.name if manager else None,
            "roles": sorted(current.roles),
        }

    @application.get("/api/workflows")
    async def get_workflows(current: Principal = Depends(principal)) -> list[dict[str, Any]]:
        del current
        return [workflow.model_dump() for workflow in spec.workflows]

    @application.get("/api/ui/resources")
    async def ui_resources(current: Principal = Depends(principal)) -> list[dict[str, Any]]:
        if current.manager_id and current.manager_id != agent_identity.config.AGENT_MANAGER_ID:
            raise HTTPException(status_code=403, detail="This Agent ID is assigned to another manager")
        return runtime.user_interfaces.available(current.manager_id, current.roles)

    @application.post("/api/ui/{resource_id}/query")
    async def query_ui_resource(
        resource_id: str,
        payload: UiQuery,
        current: Principal = Depends(principal),
    ) -> dict[str, Any]:
        if current.manager_id and current.manager_id != agent_identity.config.AGENT_MANAGER_ID:
            raise HTTPException(status_code=403, detail="This Agent ID is assigned to another manager")
        try:
            return runtime.user_interfaces.snapshot(
                resource_id, current.manager_id, current.roles, payload
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @application.post(spec.user_interfaces.ag_ui.endpoint)
    async def ag_ui_events(
        payload: RunAgentInput,
        request: Request,
        current: Principal = Depends(principal),
    ) -> StreamingResponse:
        if current.manager_id and current.manager_id != agent_identity.config.AGENT_MANAGER_ID:
            raise HTTPException(status_code=403, detail="This Agent ID is assigned to another manager")

        async def authenticated_events():
            identity_token = None
            if current.manager_id:
                identity_token = agent_identity.set_context(
                    agent_identity.request_context(
                        current.manager_id,
                        payload.thread_id,
                        principal_id=current.principal_id,
                        inbound_assertion=current.inbound_assertion,
                    )
                )
            scope = None
            try:
                with observability.invoke_agent_scope(
                    f"AG-UI {payload.thread_id}",
                    session_id=f"ag-ui:{current.manager_id or current.principal_id}",
                    conversation_id=payload.thread_id,
                ) as scope:
                    async for event in runtime.ag_ui.stream(
                        payload,
                        manager_id=current.manager_id,
                        roles=current.roles,
                        accept=request.headers.get("accept"),
                    ):
                        yield event
                    observability.record_response(scope, {"runId": payload.run_id})
            except Exception as exc:
                observability.record_error(scope, exc)
                raise
            finally:
                if identity_token is not None:
                    agent_identity.reset_context(identity_token)
                observability.force_flush()

        return StreamingResponse(
            authenticated_events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @application.get("/api/manager/summary")
    async def manager_summary(current: Principal = Depends(principal)) -> dict[str, Any]:
        if not current.manager_id:
            raise HTTPException(status_code=403, detail="Manager assignment required")
        if current.manager_id != agent_identity.config.AGENT_MANAGER_ID:
            raise HTTPException(status_code=403, detail="This Agent ID is assigned to another manager")
        manager = manager_for(spec, current.manager_id)
        return {
            "manager": manager.model_dump(),
            "terms": spec.solution.terms.model_dump(),
            "summaryFields": spec.control_plane.manager.summary_fields,
            "subjects": data.manager_subjects(manager.id),
            "workflows": [workflow.model_dump() for workflow in spec.workflows],
            "runs": state_store.list_runs(manager.id),
            "reviews": state_store.list_reviews(manager.id, pending_only=True),
        }

    @application.get("/api/fleet/summary")
    async def fleet_summary(current: Principal = Depends(principal)) -> dict[str, Any]:
        require_fleet(current, spec)
        runs = state_store.list_runs()
        reviews = state_store.list_reviews(pending_only=True)
        managers: list[dict[str, Any]] = []
        for manager in spec.managers:
            manager_runs = [run for run in runs if run["managerId"] == manager.id]
            manager_reviews = [review for review in reviews if review["managerId"] == manager.id]
            managers.append(
                {
                    **manager.model_dump(),
                    "subjectCount": len(data.manager_subjects(manager.id)),
                    "activeRuns": sum(run["status"] == "running" for run in manager_runs),
                    "pendingReviews": len(manager_reviews),
                }
            )
        metrics = {
            "managers": len(spec.managers),
            "subjects": len(data.all_subjects()),
            "active_runs": sum(run["status"] == "running" for run in runs),
            "pending_reviews": len(reviews),
        }
        return {"metrics": metrics, "managers": managers, "runs": runs, "reviews": reviews}

    @application.post("/api/workflows/{workflow_id}/runs", status_code=status.HTTP_201_CREATED)
    async def start_run(
        workflow_id: str,
        payload: RunRequest,
        request: Request,
        current: Principal = Depends(principal),
    ) -> dict[str, Any]:
        manager_id = payload.manager_id or current.manager_id
        if not manager_id:
            raise HTTPException(status_code=403, detail="Manager assignment required")
        require_manager(current, spec, manager_id)
        if manager_id != agent_identity.config.AGENT_MANAGER_ID:
            raise HTTPException(status_code=403, detail="This Agent ID is assigned to another manager")
        request_key = request.headers.get("idempotency-key", "").strip()
        if not request_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        identity_token = agent_identity.set_context(
            agent_identity.request_context(
                manager_id,
                request.headers.get("x-conversation-id", "control-plane"),
                principal_id=current.principal_id,
                inbound_assertion=current.inbound_assertion,
            )
        )
        scope = None
        try:
            with observability.invoke_agent_scope(
                f"Start workflow {workflow_id}",
                session_id=f"control-plane:{manager_id}",
                conversation_id=request.headers.get("x-conversation-id", "control-plane"),
            ) as scope:
                result = await workflows.start(
                    workflow_id,
                    manager_id,
                    payload.subject_id,
                    payload.trigger_mode,
                    payload.input,
                    request_key=request_key,
                )
                observability.record_response(scope, result)
                return result
        except KeyError as exc:
            observability.record_error(scope, exc)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            observability.record_error(scope, exc)
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            observability.record_error(scope, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            agent_identity.reset_context(identity_token)
            observability.force_flush()

    @application.get("/api/runs/{run_id}")
    async def get_run(run_id: str, current: Principal = Depends(principal)) -> dict[str, Any]:
        run = state_store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        require_manager_read(current, spec, run["managerId"])
        return run

    @application.get("/api/reviews")
    async def get_reviews(current: Principal = Depends(principal)) -> list[dict[str, Any]]:
        if current.manager_id != agent_identity.config.AGENT_MANAGER_ID:
            raise HTTPException(status_code=403, detail="This Agent ID is assigned to another manager")
        return state_store.list_reviews(current.manager_id, pending_only=True)

    @application.post("/api/reviews/{review_id}")
    async def decide_review(
        review_id: str,
        payload: ReviewRequest,
        request: Request,
        current: Principal = Depends(principal),
    ) -> dict[str, Any]:
        review = state_store.get_review(review_id)
        if review is None:
            raise HTTPException(status_code=404, detail="Review not found")
        require_manager(current, spec, review["managerId"])
        if review["managerId"] != agent_identity.config.AGENT_MANAGER_ID:
            raise HTTPException(status_code=403, detail="This Agent ID is assigned to another manager")
        identity_token = agent_identity.set_context(
            agent_identity.request_context(
                review["managerId"],
                request.headers.get("x-conversation-id", "control-plane"),
                principal_id=current.principal_id,
                inbound_assertion=current.inbound_assertion,
            )
        )
        scope = None
        try:
            with observability.invoke_agent_scope(
                f"Resolve review {review_id}",
                session_id=f"control-plane:{review['managerId']}",
                conversation_id=request.headers.get("x-conversation-id", "control-plane"),
            ) as scope:
                result = await workflows.resolve_review(
                    review_id, review["managerId"], payload.decision, payload.final
                )
                observability.record_response(scope, result)
                return result
        except KeyError as exc:
            observability.record_error(scope, exc)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            observability.record_error(scope, exc)
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            observability.record_error(scope, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            agent_identity.reset_context(identity_token)
            observability.force_flush()

    @application.get("/manager", response_class=HTMLResponse, include_in_schema=False)
    async def manager_page() -> str:
        return control_plane_page(spec, "manager")

    @application.get("/fleet", response_class=HTMLResponse, include_in_schema=False)
    async def fleet_page() -> str:
        return control_plane_page(spec, "fleet")

    return application


app = create_app()
