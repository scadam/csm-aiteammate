from __future__ import annotations

from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from app.data import DataCatalog
from app.main import create_app
from app.spec import load_spec


ROOT = Path(__file__).resolve().parents[1]
SPEC = load_spec(ROOT / "solution.yaml")
WORKFLOW = SPEC.workflows[0]
DEFAULT_MANAGER = next(
    item for item in SPEC.managers if item.id == SPEC.identity.default_manager_id
)
DATA = DataCatalog(SPEC)
SUBJECT = DATA.manager_subjects(DEFAULT_MANAGER.id)[0]
REVIEW_INPUT = WORKFLOW.test_cases.review_input if WORKFLOW.test_cases else {}
AUTO_INPUT = WORKFLOW.test_cases.automatic_input if WORKFLOW.test_cases else {}


@pytest.fixture
def client(tmp_path: Path):
    application = create_app(spec_path=ROOT / "solution.yaml", state_path=tmp_path / "state.db")
    with TestClient(application) as test_client:
        yield test_client


def test_health_spec_and_dynamic_pages(client: TestClient):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["solution"] == SPEC.solution.id
    assert health.json()["status"] == "offline"
    assert health.json()["liveReady"] is False

    public_spec = client.get("/api/spec").json()
    assert public_spec["solution"]["terms"]["subject_plural"] == SPEC.solution.terms.subject_plural
    assert "records" not in str(public_spec)

    assert SPEC.control_plane.manager.title in client.get("/manager").text
    assert SPEC.control_plane.fleet.title in client.get("/fleet").text
    manager_page = client.get("/manager").text
    assert "MicrosoftTeams.min.js" in manager_page
    assert "getAuthToken" in manager_page
    assert "headers.Authorization=`Bearer ${IDENTITY_TOKEN}`" in manager_page
    assert "teams.app.notifySuccess" in manager_page
    assert "getAuthToken(),TEAMS_TIMEOUT_MS" in manager_page
    assert "'/api/ag-ui'" in manager_page
    assert "data-ui-bulk" in manager_page
    assert "Promise.all(UI_RESOURCES.filter" in manager_page
    assert "await(VIEW==='manager'?manager():fleet())" in manager_page
    response = client.get("/manager")
    assert "teams.cloud.microsoft" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert "x-frame-options" not in response.headers


def test_manager_scope_and_fleet_authorization(client: TestClient):
    summary = client.get("/api/manager/summary").json()
    assert summary["manager"]["id"] == DEFAULT_MANAGER.id
    assert SUBJECT["subjectId"] in [subject["subjectId"] for subject in summary["subjects"]]

    other_manager = next(
        (item for item in SPEC.managers if item.id != DEFAULT_MANAGER.id), None
    )
    if other_manager:
        sam_headers = {
            "x-principal-id": other_manager.principal_id,
            "x-manager-id": other_manager.id,
            "x-roles": ",".join(other_manager.roles),
        }
        assert client.get("/api/fleet/summary", headers=sam_headers).status_code == 403
    else:
        sam_headers = {
            "x-principal-id": "undeclared-principal",
            "x-manager-id": DEFAULT_MANAGER.id,
            "x-roles": "manager",
        }
        assert client.get("/api/manager/summary", headers=sam_headers).status_code == 403
    assert client.get("/api/fleet/summary").json()["metrics"]["managers"] == len(SPEC.managers)

    if other_manager:
        response = client.post(
            f"/api/workflows/{WORKFLOW.id}/runs",
            headers=sam_headers,
            json={"subject_id": SUBJECT["subjectId"], "trigger_mode": "human", "input": {}},
        )
        assert response.status_code == 403

    fleet_principal = SPEC.identity.fleet_principals[0]
    fleet_headers = {
        "x-principal-id": fleet_principal.principal_id,
        "x-manager-id": DEFAULT_MANAGER.id,
        "x-roles": ",".join(fleet_principal.roles),
    }
    assert client.get("/api/fleet/summary", headers=fleet_headers).status_code == 200
    assert client.get("/api/manager/summary", headers=fleet_headers).status_code == 403
    assert client.post(
        f"/api/workflows/{WORKFLOW.id}/runs",
        headers={**fleet_headers, "idempotency-key": "fleet-mutation"},
        json={"subject_id": SUBJECT["subjectId"], "trigger_mode": "human", "input": {}},
    ).status_code == 403


def test_workflow_pauses_for_review_and_resumes(client: TestClient):
    started = client.post(
        f"/api/workflows/{WORKFLOW.id}/runs",
        headers={"idempotency-key": "review-run"},
        json={
            "subject_id": SUBJECT["subjectId"],
            "trigger_mode": "human",
            "input": REVIEW_INPUT,
        },
    )
    assert started.status_code == 201
    run = started.json()
    assert run["status"] == "pending_review"
    assert any(result.get("provenance", "").startswith("offline:") for result in run["results"])

    reviews = client.get("/api/reviews").json()
    assert len(reviews) == 1
    review_id = reviews[0]["id"]
    proposed = reviews[0]["context"].get("proposedEffect") or {}
    resolved = client.post(
        f"/api/reviews/{review_id}",
        json={
            "decision": "approve",
            "final": proposed.get("arguments", {}),
        },
    )
    assert resolved.status_code == 200
    completed = resolved.json()
    assert completed["status"] == "complete"
    assert completed["results"][-1]["stageId"] == WORKFLOW.stages[-1].id
    effects = [result for result in completed["results"] if result.get("side_effect")]
    if effects:
        assert effects[0]["idempotency_key"]


def _sse_events(response) -> list[dict]:
    return [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]


def test_generated_ui_resources_and_ag_ui_interrupt_resume(client: TestClient):
    started = client.post(
        f"/api/workflows/{WORKFLOW.id}/runs",
        headers={"idempotency-key": "ag-ui-review-run"},
        json={
            "subject_id": SUBJECT["subjectId"],
            "trigger_mode": "human",
            "input": REVIEW_INPUT,
        },
    )
    assert started.status_code == 201
    resources = client.get("/api/ui/resources").json()
    manager_resources = [item for item in resources if item["audience"] == "manager"]
    progress = next(item for item in manager_resources if item["kind"] == "dashboard")
    approvals = next(item for item in manager_resources if item["kind"] == "hitl")
    query = client.post(
        f"/api/ui/{progress['id']}/query",
        json={"filters": {"status": "pending"}, "sort_field": "updatedAt", "sort_direction": "desc"},
    )
    assert query.status_code == 200
    assert query.json()["items"][0]["status"] == "pending_review"

    thread_id = "control-plane:manager:approvals:principal"
    body = {
        "threadId": thread_id,
        "runId": "ag-ui-run-1",
        "state": {"resourceId": approvals["id"], "query": {}},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {"surface": "test"},
    }
    streamed = client.post("/api/ag-ui", headers={"accept": "text/event-stream"}, json=body)
    assert streamed.status_code == 200
    events = _sse_events(streamed)
    assert events[0]["type"] == "RUN_STARTED"
    snapshot = next(event["snapshot"] for event in events if event["type"] == "STATE_SNAPSHOT")
    assert snapshot["items"] and snapshot["items"][0]["digest"]
    activity = next(event for event in events if event["type"] == "ACTIVITY_SNAPSHOT")
    assert activity["activityType"] == "HITL_REVIEW"
    finished = events[-1]
    assert finished["type"] == "RUN_FINISHED"
    assert finished["outcome"]["type"] == "interrupt"
    interrupt = finished["outcome"]["interrupts"][0]
    item = snapshot["items"][0]

    resumed = client.post(
        "/api/ag-ui",
        headers={"accept": "text/event-stream"},
        json={
            **body,
            "runId": "ag-ui-run-2",
            "resume": [
                {
                    "interruptId": interrupt["id"],
                    "status": "resolved",
                    "payload": {
                        "decisions": [
                            {
                                "review_id": item["id"],
                                "expected_digest": item["digest"],
                                "decision": "approve",
                                "final": item["proposedEffect"],
                            }
                        ]
                    },
                }
            ],
        },
    )
    resumed_events = _sse_events(resumed)
    assert resumed_events[-1]["type"] == "RUN_FINISHED"
    assert resumed_events[-1]["outcome"]["type"] == "success"
    assert client.get(f"/api/runs/{started.json()['id']}").json()["status"] == "complete"


def test_ag_ui_rejects_stale_review_digest(client: TestClient):
    client.post(
        f"/api/workflows/{WORKFLOW.id}/runs",
        headers={"idempotency-key": "ag-ui-stale-run"},
        json={"subject_id": SUBJECT["subjectId"], "trigger_mode": "human", "input": REVIEW_INPUT},
    )
    approvals = next(
        item for item in client.get("/api/ui/resources").json() if item["kind"] == "hitl"
    )
    thread_id = "stale-digest-thread"
    body = {
        "threadId": thread_id,
        "runId": "stale-run-1",
        "state": {"resourceId": approvals["id"], "query": {}},
        "messages": [], "tools": [], "context": [], "forwardedProps": {},
    }
    initial = _sse_events(client.post("/api/ag-ui", json=body))
    snapshot = next(event["snapshot"] for event in initial if event["type"] == "STATE_SNAPSHOT")
    interrupt = initial[-1]["outcome"]["interrupts"][0]
    item = snapshot["items"][0]
    rejected = _sse_events(
        client.post(
            "/api/ag-ui",
            json={
                **body,
                "runId": "stale-run-2",
                "resume": [{
                    "interruptId": interrupt["id"],
                    "status": "resolved",
                    "payload": {"decisions": [{
                        "review_id": item["id"],
                        "expected_digest": "stale",
                        "decision": "approve",
                    }]},
                }],
            },
        )
    )
    assert rejected[-1]["type"] == "RUN_ERROR"
    assert "digest" in rejected[-1]["message"].lower()


def test_ag_ui_bulk_prevalidation_prevents_partial_review_mutation(client: TestClient):
    for index in range(2):
        response = client.post(
            f"/api/workflows/{WORKFLOW.id}/runs",
            headers={"idempotency-key": f"ag-ui-atomic-{index}"},
            json={"subject_id": SUBJECT["subjectId"], "trigger_mode": "human", "input": REVIEW_INPUT},
        )
        assert response.status_code == 201
    approvals = next(
        item for item in client.get("/api/ui/resources").json() if item["kind"] == "hitl"
    )
    thread_id = "atomic-bulk-thread"
    body = {
        "threadId": thread_id,
        "runId": "atomic-1",
        "state": {"resourceId": approvals["id"], "query": {}},
        "messages": [], "tools": [], "context": [], "forwardedProps": {},
    }
    initial = _sse_events(client.post("/api/ag-ui", json=body))
    snapshot = next(event["snapshot"] for event in initial if event["type"] == "STATE_SNAPSHOT")
    items = snapshot["items"][:2]
    interrupt = initial[-1]["outcome"]["interrupts"][0]
    events = _sse_events(
        client.post(
            "/api/ag-ui",
            json={
                **body,
                "runId": "atomic-2",
                "resume": [{
                    "interruptId": interrupt["id"],
                    "status": "resolved",
                    "payload": {"decisions": [
                        {
                            "review_id": items[0]["id"],
                            "expected_digest": items[0]["digest"],
                            "decision": "approve",
                        },
                        {
                            "review_id": items[1]["id"],
                            "expected_digest": "stale",
                            "decision": "approve",
                        },
                    ]},
                }],
            },
        )
    )
    assert events[-1]["type"] == "RUN_ERROR"
    pending_ids = {item["id"] for item in client.get("/api/reviews").json()}
    assert {item["id"] for item in items}.issubset(pending_ids)


def test_low_severity_skips_review(client: TestClient):
    response = client.post(
        f"/api/workflows/{WORKFLOW.id}/runs",
        headers={"idempotency-key": "low-severity-run"},
        json={
            "subject_id": SUBJECT["subjectId"],
            "trigger_mode": "human",
            "input": AUTO_INPUT,
        },
    )
    assert response.status_code == 201
    run = response.json()
    assert run["status"] == "complete"
    review_stage_id = next(stage.id for stage in WORKFLOW.stages if stage.type == "review")
    review_stage = next(result for result in run["results"] if result["stageId"] == review_stage_id)
    assert review_stage["status"] == "skipped"


def test_workflow_start_requires_and_replays_idempotency_key(client: TestClient):
    payload = {
        "subject_id": SUBJECT["subjectId"],
        "trigger_mode": "human",
        "input": AUTO_INPUT,
    }
    url = f"/api/workflows/{WORKFLOW.id}/runs"
    assert client.post(url, json=payload).status_code == 400
    headers = {"idempotency-key": "stable-request"}
    first = client.post(url, headers=headers, json=payload)
    second = client.post(url, headers=headers, json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]