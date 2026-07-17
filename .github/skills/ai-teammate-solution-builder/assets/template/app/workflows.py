"""Specification-driven workflow execution and deterministic review gates."""

from __future__ import annotations

import hashlib
from typing import Any

from . import config
from .capabilities import CapabilityRegistry
from .data import DataCatalog
from .spec import Condition, Manager, SolutionSpec, Workflow
from .state import StateStore


class WorkflowEngine:
    def __init__(
        self,
        spec: SolutionSpec,
        data: DataCatalog,
        state: StateStore,
        capabilities: CapabilityRegistry,
    ):
        self.spec = spec
        self.data = data
        self.state = state
        self.capabilities = capabilities
        self.workflows = {workflow.id: workflow for workflow in spec.workflows}
        self.managers = {manager.id: manager for manager in spec.managers}

    async def start(
        self,
        workflow_id: str,
        manager_id: str,
        subject_id: str,
        trigger_mode: str,
        input_data: dict[str, Any],
        request_key: str = "",
    ) -> dict[str, Any]:
        workflow = self._workflow(workflow_id)
        if trigger_mode not in workflow.trigger_modes:
            raise ValueError(f"Trigger mode {trigger_mode!r} is not allowed for {workflow_id}")
        manager = self._manager(manager_id)
        subject = self.data.subject(workflow.subject_source, subject_id)
        if subject is None:
            raise KeyError(f"Unknown subject {subject_id}")
        source = self.data.sources[workflow.subject_source]
        if str(subject.get(source.manager_field, "")) != manager_id:
            raise PermissionError("Subject is outside the manager scope")
        run = self.state.create_run(
            workflow_id,
            manager_id,
            subject_id,
            trigger_mode,
            input_data,
            request_key=_request_key(workflow_id, manager_id, request_key),
        )
        if run["status"] != "running" or run["results"]:
            return run
        return await self._execute(run, workflow, manager, subject, start_index=0)

    async def resolve_review(
        self,
        review_id: str,
        manager_id: str,
        decision: str,
        final_data: dict[str, Any],
    ) -> dict[str, Any]:
        review = self.state.get_review(review_id)
        if review is None:
            raise KeyError(review_id)
        if review["managerId"] != manager_id:
            raise PermissionError("Review is outside the manager scope")
        if review["status"] != "pending":
            if review.get("decision") == decision and review.get("final") == final_data:
                run = self.state.get_run(review["runId"])
                if run is None:
                    raise KeyError(review["runId"])
                return run
            raise ValueError("Review has already been resolved")
        proposed = review["context"].get("proposedEffect")
        approved_arguments: dict[str, Any] | None = None
        approved_digest = ""
        if decision in {"approve", "edit"} and proposed:
            candidate = proposed["arguments"] if decision == "approve" else final_data
            approved_arguments = self.capabilities.validate_arguments(
                proposed["capabilityId"], candidate
            )
            approved_digest = self.capabilities.review_digest(
                proposed["capabilityId"], approved_arguments
            )
        review = self.state.decide_review(review_id, decision, final_data)
        run = self.state.get_run(review["runId"])
        if run is None:
            raise KeyError(review["runId"])
        if decision == "reject":
            return self.state.save_run(run["id"], "rejected", run["results"])
        if decision == "defer":
            return self.state.save_run(run["id"], "deferred", run["results"])
        workflow = self._workflow(run["workflowId"])
        manager = self._manager(run["managerId"])
        subject = self.data.subject(workflow.subject_source, run["subjectId"])
        if subject is None:
            raise KeyError(run["subjectId"])
        results = list(run["results"])
        results.append(
            {
                "stageId": "review_decision",
                "title": "Review decision",
                "status": "done",
                "decision": decision,
                "final": final_data,
                "provenance": "human:review",
            }
        )
        run = self.state.save_run(run["id"], "running", results)
        return await self._execute(
            run,
            workflow,
            manager,
            subject,
            start_index=int(review["context"]["nextStageIndex"]),
            approved_effect_digest=approved_digest,
            approved_stage_id=(proposed or {}).get("stageId", ""),
            approved_arguments=approved_arguments,
        )

    async def _execute(
        self,
        run: dict[str, Any],
        workflow: Workflow,
        manager: Manager,
        subject: dict[str, Any],
        *,
        start_index: int,
        approved_effect_digest: str = "",
        approved_stage_id: str = "",
        approved_arguments: dict[str, Any] | None = None,
        review_policy_cleared: bool = False,
    ) -> dict[str, Any]:
        results = list(run["results"])
        for index in range(start_index, len(workflow.stages)):
            stage = workflow.stages[index]
            context = self._context(
                run,
                manager,
                subject,
                results,
                approved_effect_digest,
                review_policy_cleared,
            )
            if stage.type == "review":
                if self._requires_review(workflow, context):
                    proposed_effect = self._proposed_effect(
                        workflow, index + 1, context
                    )
                    review = self.state.create_review(
                        run["id"],
                        manager.id,
                        list(workflow.review.decisions),
                        {
                            "nextStageIndex": index + 1,
                            "stageId": stage.id,
                            "proposedEffect": proposed_effect,
                        },
                    )
                    results.append(
                        {
                            "stageId": stage.id,
                            "title": stage.title,
                            "status": "pending_review",
                            "reviewId": review["id"],
                            "provenance": "policy:deterministic",
                        }
                    )
                    return self.state.save_run(run["id"], "pending_review", results)
                results.append(
                    {
                        "stageId": stage.id,
                        "title": stage.title,
                        "status": "skipped",
                        "provenance": "policy:deterministic",
                    }
                )
                review_policy_cleared = True
                run = self.state.save_run(run["id"], "running", results)
                continue
            try:
                if not stage.capability:
                    raise ValueError(f"Stage {stage.id} has no capability")
                if stage.id == approved_stage_id and approved_arguments is not None:
                    arguments = approved_arguments
                else:
                    arguments = _bind_arguments(stage.arguments, context)
                result = await self.capabilities.execute(
                    stage.capability, arguments, context=context, surface="workflow"
                )
                results.append(
                    {
                        "stageId": stage.id,
                        "title": stage.title,
                        **result.model_dump(mode="json"),
                        "status": "done",
                    }
                )
                run = self.state.save_run(run["id"], "running", results)
            except Exception as exc:
                results.append(
                    {
                        "stageId": stage.id,
                        "title": stage.title,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                if stage.on_error == "continue":
                    run = self.state.save_run(run["id"], "running", results)
                    continue
                if stage.on_error == "review":
                    review = self.state.create_review(
                        run["id"],
                        manager.id,
                        list(workflow.review.decisions),
                        {"nextStageIndex": index + 1, "stageId": stage.id, "reason": str(exc)},
                    )
                    results[-1]["reviewId"] = review["id"]
                    return self.state.save_run(run["id"], "pending_review", results)
                return self.state.save_run(run["id"], "failed", results)
        return self.state.save_run(run["id"], "complete", results)

    def _context(
        self,
        run: dict[str, Any],
        manager: Manager,
        subject: dict[str, Any],
        results: list[dict[str, Any]],
        approved_effect_digest: str,
        review_policy_cleared: bool,
    ) -> dict[str, Any]:
        return {
            "runId": run["id"],
            "workflowId": run["workflowId"],
            "manager": manager.model_dump(),
            "subjectId": run["subjectId"],
            "subject": subject,
            "input": run["input"],
            "results": {result["stageId"]: result for result in results},
            "approvedEffectDigest": approved_effect_digest,
            "reviewPolicyCleared": review_policy_cleared,
        }

    def _proposed_effect(
        self, workflow: Workflow, start_index: int, context: dict[str, Any]
    ) -> dict[str, Any] | None:
        for stage in workflow.stages[start_index:]:
            if not stage.capability:
                continue
            capability = self.capabilities.capabilities[stage.capability]
            if not capability.side_effect or capability.review_mode == "none":
                continue
            arguments = _bind_arguments(stage.arguments, context)
            validated = self.capabilities.validate_arguments(capability.id, arguments)
            return {
                "stageId": stage.id,
                "capabilityId": capability.id,
                "arguments": validated,
                "digest": self.capabilities.review_digest(capability.id, validated),
            }
        return None

    @staticmethod
    def _requires_review(workflow: Workflow, context: dict[str, Any]) -> bool:
        return any(_matches(condition, context) for condition in workflow.review.required_when)

    def _workflow(self, workflow_id: str) -> Workflow:
        workflow = self.workflows.get(workflow_id)
        if workflow is None:
            raise KeyError(f"Unknown workflow {workflow_id}")
        return workflow

    def _manager(self, manager_id: str) -> Manager:
        manager = self.managers.get(manager_id)
        if manager is None:
            raise KeyError(f"Unknown manager {manager_id}")
        return manager


def _matches(condition: Condition, context: dict[str, Any]) -> bool:
    actual, exists = _resolve(context, condition.field)
    expected = condition.value
    if condition.operator == "exists":
        return exists is bool(expected)
    if not exists:
        return False
    if condition.operator == "eq":
        return actual == expected
    if condition.operator == "ne":
        return actual != expected
    if condition.operator == "in":
        return actual in expected
    if condition.operator == "not_in":
        return actual not in expected
    if condition.operator == "gt":
        return actual > expected
    if condition.operator == "gte":
        return actual >= expected
    if condition.operator == "lt":
        return actual < expected
    if condition.operator == "lte":
        return actual <= expected
    return False


def _resolve(value: Any, dotted: str) -> tuple[Any, bool]:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None, False
        current = current[part]
    return current, True


def _bind_arguments(template: Any, context: dict[str, Any]) -> Any:
    if isinstance(template, dict):
        if set(template) == {"from"}:
            value, exists = _resolve(context, str(template["from"]))
            if not exists:
                raise ValueError(f"Workflow argument source {template['from']!r} does not exist")
            return value
        return {key: _bind_arguments(value, context) for key, value in template.items()}
    if isinstance(template, list):
        return [_bind_arguments(value, context) for value in template]
    return template


def _request_key(workflow_id: str, manager_id: str, caller_key: str) -> str:
    if not caller_key:
        return ""
    value = ":".join(
        [
            config.AGENT_ID or config.AGENT_INSTANCE_APP_ID or "development-agent",
            manager_id,
            workflow_id,
            caller_key,
        ]
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
