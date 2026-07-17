"""AG-UI HTTP/SSE adapter over generated workflow and review state."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import (
    ActivitySnapshotEvent,
    EventType,
    Interrupt,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
)
from ag_ui.encoder import EventEncoder

from .spec import UiResource
from .user_interfaces import UiQuery, UserInterfaceService


class AgUiAdapter:
    def __init__(self, service: UserInterfaceService, workflow_engine: Any):
        self.service = service
        self.workflow_engine = workflow_engine

    async def stream(
        self,
        input_data: RunAgentInput,
        *,
        manager_id: str | None,
        roles: set[str],
        accept: str | None = None,
    ) -> AsyncIterator[str]:
        encoder = EventEncoder(accept=accept)
        yield encoder.encode(
            RunStartedEvent(
                type=EventType.RUN_STARTED,
                thread_id=input_data.thread_id,
                run_id=input_data.run_id,
                parent_run_id=input_data.parent_run_id,
                input=input_data,
            )
        )
        try:
            resource, query = self._request(input_data)
            if input_data.resume:
                if not manager_id:
                    raise PermissionError("Review resume requires the assigned manager")
                before = self.service.snapshot(resource.id, manager_id, roles, query)
                expected_interrupt_id = self.service.interrupt_id(
                    resource.id, manager_id, input_data.thread_id, before["items"]
                )
                await self._resume(input_data, manager_id, expected_interrupt_id)
            yield encoder.encode(
                StepStartedEvent(type=EventType.STEP_STARTED, step_name="project_ui_state")
            )
            snapshot = self.service.snapshot(resource.id, manager_id, roles, query)
            yield encoder.encode(
                StateSnapshotEvent(
                    type=EventType.STATE_SNAPSHOT,
                    snapshot={
                        "resourceId": resource.id,
                        "resource": snapshot["resource"],
                        "metrics": snapshot["metrics"],
                        "items": snapshot["items"],
                        "pagination": {
                            "total": snapshot["total"],
                            "page": snapshot["page"],
                            "pageSize": snapshot["pageSize"],
                            "hasMore": snapshot["hasMore"],
                        },
                    },
                )
            )
            yield encoder.encode(
                ActivitySnapshotEvent(
                    type=EventType.ACTIVITY_SNAPSHOT,
                    message_id=f"activity:{resource.id}:{input_data.thread_id}",
                    activity_type="HITL_REVIEW" if resource.kind == "hitl" else "WORKFLOW_PROGRESS",
                    content={
                        "title": resource.title,
                        "metrics": snapshot["metrics"],
                        "items": snapshot["items"],
                        "generatedAt": snapshot["generatedAt"],
                    },
                )
            )
            yield encoder.encode(
                StepFinishedEvent(type=EventType.STEP_FINISHED, step_name="project_ui_state")
            )
            pending = snapshot["items"] if resource.kind == "hitl" else []
            if pending:
                if not manager_id:
                    raise PermissionError("HITL state requires the assigned manager")
                interrupt_id = self.service.interrupt_id(
                    resource.id, manager_id, input_data.thread_id, pending
                )
                yield encoder.encode(
                    RunFinishedEvent(
                        type=EventType.RUN_FINISHED,
                        thread_id=input_data.thread_id,
                        run_id=input_data.run_id,
                        result=snapshot,
                        outcome=RunFinishedInterruptOutcome(
                            interrupts=[
                                Interrupt(
                                    id=interrupt_id,
                                    reason="input_required",
                                    message=f"Resolve {len(pending)} pending {resource.title} items.",
                                    response_schema={
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "decisions": {
                                                "type": "array",
                                                "minItems": 1,
                                                "items": {
                                                    "type": "object",
                                                    "required": [
                                                        "review_id",
                                                        "expected_digest",
                                                        "decision",
                                                    ],
                                                    "properties": {
                                                        "review_id": {"type": "string"},
                                                        "expected_digest": {"type": "string"},
                                                        "decision": {
                                                            "enum": [
                                                                "approve",
                                                                "edit",
                                                                "reject",
                                                                "defer",
                                                            ]
                                                        },
                                                        "final": {"type": "object"},
                                                    },
                                                },
                                            }
                                        },
                                        "required": ["decisions"],
                                    },
                                    metadata={
                                        "resourceId": resource.id,
                                        "reviewDigests": {
                                            item["id"]: item["digest"] for item in pending
                                        },
                                    },
                                )
                            ]
                        ),
                    )
                )
                return
            yield encoder.encode(
                RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=input_data.thread_id,
                    run_id=input_data.run_id,
                    result=snapshot,
                    outcome=RunFinishedSuccessOutcome(),
                )
            )
        except Exception as error:
            yield encoder.encode(
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=str(error),
                    code=type(error).__name__,
                )
            )

    def _request(self, input_data: RunAgentInput) -> tuple[UiResource, UiQuery]:
        state = input_data.state if isinstance(input_data.state, dict) else {}
        resource_id = str(state.get("resourceId", ""))
        resource = self.service.resources.get(resource_id)
        if resource is None:
            raise ValueError("AG-UI state.resourceId must reference a generated UI resource")
        query = UiQuery.model_validate(state.get("query", {}))
        return resource, query

    async def _resume(
        self, input_data: RunAgentInput, manager_id: str, expected_interrupt_id: str
    ) -> None:
        if len(input_data.resume or []) != 1:
            raise ValueError("AG-UI review resume must address the single open interrupt")
        entry = input_data.resume[0]
        if entry.interrupt_id != expected_interrupt_id:
            raise ValueError("AG-UI resume does not match the open interrupt")
        if entry.status == "cancelled":
            return
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        decisions = payload.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise ValueError("AG-UI review resume requires decisions")
        review_ids = [str(item.get("review_id", "")) for item in decisions]
        if len(review_ids) != len(set(review_ids)):
            raise ValueError("AG-UI review resume cannot repeat a review ID")
        prepared: list[tuple[str, str, dict[str, Any]]] = []
        for decision in decisions:
            review_id = str(decision.get("review_id", ""))
            review = self.service.state.get_review(review_id)
            if review is None or review["managerId"] != manager_id:
                raise PermissionError("Review is outside the assigned manager scope")
            proposed = review.get("context", {}).get("proposedEffect") or {}
            if self.service.review_digest(review) != decision.get("expected_digest"):
                raise ValueError("Review effect digest is stale or does not match")
            action = str(decision.get("decision", ""))
            if action not in review["decisions"]:
                raise ValueError("Decision is not allowed by this workflow")
            final = decision.get("final", {})
            if action == "approve" and proposed:
                final = proposed.get("arguments", {})
            elif action == "edit":
                if not proposed:
                    raise ValueError("An error-recovery review cannot be edited")
                final = self.workflow_engine.capabilities.validate_arguments(
                    proposed["capabilityId"], final if isinstance(final, dict) else {}
                )
            prepared.append((review_id, action, final if isinstance(final, dict) else {}))
        for review_id, action, final in prepared:
            await self.workflow_engine.resolve_review(
                review_id, manager_id, action, final
            )
