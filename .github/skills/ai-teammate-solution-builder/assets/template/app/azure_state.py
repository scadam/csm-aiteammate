"""Managed-identity Azure Table implementation of shared workflow state."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.data.tables import TableServiceClient, UpdateMode
from azure.identity import DefaultAzureCredential


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AzureTableStateStore:
    """Shared state for horizontally scaled agent/control-plane/MCP processes."""

    def __init__(self, endpoint: str, table_name: str):
        if not endpoint.lower().startswith("https://"):
            raise ValueError("STATE_TABLE_ENDPOINT must be an HTTPS Table service endpoint")
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        service = TableServiceClient(endpoint=endpoint, credential=credential)
        self.table = service.create_table_if_not_exists(table_name=table_name)

    def create_run(
        self,
        workflow_id: str,
        manager_id: str,
        subject_id: str,
        trigger_mode: str,
        input_data: dict[str, Any],
        request_key: str = "",
    ) -> dict[str, Any]:
        if request_key:
            existing = self._get("workflow_request", request_key)
            if existing:
                return self.get_run(existing["runId"]) or {}
        now = _now()
        run = {
            "id": f"run_{uuid.uuid4().hex[:16]}",
            "workflowId": workflow_id,
            "managerId": manager_id,
            "subjectId": subject_id,
            "triggerMode": trigger_mode,
            "status": "running",
            "input": input_data,
            "results": [],
            "createdAt": now,
            "updatedAt": now,
        }
        self._create("run", run["id"], run)
        if request_key:
            try:
                self._create(
                    "workflow_request",
                    request_key,
                    {"runId": run["id"], "createdAt": now},
                )
            except ResourceExistsError:
                existing = self._get("workflow_request", request_key)
                return self.get_run(existing["runId"]) if existing else run
        return run

    def save_run(self, run_id: str, status: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        run.update({"status": status, "results": results, "updatedAt": _now()})
        self._upsert("run", run_id, run)
        return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._get("run", run_id)

    def list_runs(self, manager_id: str | None = None) -> list[dict[str, Any]]:
        rows = self._list("run")
        if manager_id:
            rows = [row for row in rows if row["managerId"] == manager_id]
        return sorted(rows, key=lambda item: item["createdAt"], reverse=True)

    def create_review(
        self, run_id: str, manager_id: str, decisions: list[str], context: dict[str, Any]
    ) -> dict[str, Any]:
        now = _now()
        review = {
            "id": f"review_{uuid.uuid4().hex[:16]}",
            "runId": run_id,
            "managerId": manager_id,
            "status": "pending",
            "decisions": decisions,
            "context": context,
            "decision": None,
            "final": {},
            "createdAt": now,
            "updatedAt": now,
        }
        self._create("review", review["id"], review)
        return review

    def get_review(self, review_id: str) -> dict[str, Any] | None:
        return self._get("review", review_id)

    def list_reviews(
        self, manager_id: str | None = None, pending_only: bool = False
    ) -> list[dict[str, Any]]:
        rows = self._list("review")
        if manager_id:
            rows = [row for row in rows if row["managerId"] == manager_id]
        if pending_only:
            rows = [row for row in rows if row["status"] == "pending"]
        return sorted(rows, key=lambda item: item["createdAt"], reverse=True)

    def decide_review(
        self, review_id: str, decision: str, final_data: dict[str, Any]
    ) -> dict[str, Any]:
        review = self.get_review(review_id)
        if review is None:
            raise KeyError(review_id)
        if review["status"] != "pending":
            raise ValueError("Review has already been resolved")
        if decision not in review["decisions"]:
            raise ValueError("Decision is not allowed by this workflow")
        review.update(
            {
                "status": "resolved",
                "decision": decision,
                "final": final_data,
                "updatedAt": _now(),
            }
        )
        etag = review.pop("_etag", None)
        entity = self._entity("review", review_id, review)
        try:
            self.table.update_entity(
                entity,
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except ResourceModifiedError as exc:
            raise ValueError("Review has already been resolved") from exc
        return review

    def claim_effect(self, key: str, capability_id: str) -> tuple[bool, dict[str, Any] | None]:
        now = _now()
        try:
            self._create(
                "effect",
                key,
                {
                    "capabilityId": capability_id,
                    "status": "pending",
                    "result": None,
                    "createdAt": now,
                    "updatedAt": now,
                },
            )
            return True, None
        except ResourceExistsError:
            entity = self.table.get_entity("effect", key)
            effect = json.loads(entity["payload"])
            if effect.get("status") == "failed":
                effect.update({"status": "pending", "result": None, "updatedAt": _now()})
                try:
                    self.table.update_entity(
                        self._entity("effect", key, effect),
                        mode=UpdateMode.REPLACE,
                        etag=entity.metadata.get("etag"),
                        match_condition=MatchConditions.IfNotModified,
                    )
                    return True, None
                except ResourceModifiedError:
                    return False, None
            return False, effect.get("result") if effect.get("status") == "complete" else None

    def complete_effect(self, key: str, result: Any) -> None:
        effect = self._get("effect", key) or {}
        effect.update({"status": "complete", "result": result, "updatedAt": _now()})
        self._upsert("effect", key, effect)

    def fail_effect(self, key: str, error: str) -> None:
        effect = self._get("effect", key) or {}
        effect.update({"status": "failed", "result": {"error": error}, "updatedAt": _now()})
        self._upsert("effect", key, effect)

    def record_event(
        self, capability_id: str, manager_id: str, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        event = {
            "id": f"event_{uuid.uuid4().hex[:16]}",
            "capabilityId": capability_id,
            "managerId": manager_id,
            "runId": run_id,
            "payload": payload,
            "createdAt": _now(),
        }
        self._create("event", event["id"], event)
        return event

    @property
    def provenance(self) -> str:
        return "live:azure-table-state"

    def _entity(self, partition: str, key: str, value: dict[str, Any]) -> dict[str, Any]:
        return {
            "PartitionKey": partition,
            "RowKey": key,
            "payload": json.dumps(value, separators=(",", ":"), default=str),
        }

    def _create(self, partition: str, key: str, value: dict[str, Any]) -> None:
        self.table.create_entity(self._entity(partition, key, value))

    def _upsert(self, partition: str, key: str, value: dict[str, Any]) -> None:
        self.table.upsert_entity(self._entity(partition, key, value), mode=UpdateMode.REPLACE)

    def _get(self, partition: str, key: str) -> dict[str, Any] | None:
        try:
            entity = self.table.get_entity(partition, key)
        except ResourceNotFoundError:
            return None
        value = json.loads(entity["payload"])
        if partition == "review":
            value["_etag"] = entity.metadata.get("etag")
        return value

    def _list(self, partition: str) -> list[dict[str, Any]]:
        entities = self.table.query_entities(
            "PartitionKey eq @partition", parameters={"partition": partition}
        )
        result = []
        for entity in entities:
            value = json.loads(entity["payload"])
            if partition == "review":
                value["_etag"] = entity.metadata.get("etag")
            result.append(value)
        return result
