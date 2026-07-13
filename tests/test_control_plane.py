"""Tests for the CSM Autopilot control plane (store + engine, per-manager model)."""

import asyncio

import pytest

from src import data_store
from src.control_plane import engine, store


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    # No inter-stage pause during tests.
    monkeypatch.setattr(engine, "_STAGE_PAUSE", 0)
    store.reset()
    yield
    store.reset()


def _account(account_id):
    return data_store.get("accounts", "account_id", account_id)


def _run(autopilot, account):
    async def _collect():
        return [ev async for ev, _ in engine.run_job(autopilot, account, trigger="test")]
    return asyncio.run(_collect())


# ── fleet (one autopilot per CSM) ──────────────────────────────────
def test_fleet_one_autopilot_per_manager():
    fleet = store.fleet()
    assert len(fleet) == 3
    ids = {ap["id"] for ap in fleet}
    assert ids == {"ap-csm-svasireddy", "ap-csm-cora-thomas", "ap-csm-mario-rogers"}
    siva = store.autopilot_for_manager("csm-svasireddy")
    assert siva["manager"]["name"] == "Siva Vasireddy"
    # Counts/ARR are derived from the (scaled) fixtures, not hard-coded, so the
    # autopilot always reflects the CSM's real book of business.
    siva_accounts = data_store.find("accounts", csm_manager_id="csm-svasireddy")
    assert siva["accountCount"] == len(siva_accounts)
    assert siva["accountCount"] >= 3  # a real CSM carries many accounts at scale
    assert siva["arr_gbp"] == sum(a.get("arr_gbp", 0) for a in siva_accounts)
    assert siva["status"] == "idle"


def test_pick_top_account_is_highest_severity():
    siva = store.autopilot_for_manager("csm-svasireddy")
    top = engine.pick_top_account(siva)
    assert top["account_id"] == "ACC-1001"  # Meridian — Critical signal


# ── journey ───────────────────────────────────────────────────
def test_strategic_account_routes_to_review():
    siva = store.autopilot_for_manager("csm-svasireddy")
    events = _run(siva, _account("ACC-1001"))  # Meridian — Strategic + Frustrated
    assert events[0] == "status"
    assert "outcome" in events and "done" in events
    job = store.jobs()[0]
    assert job["status"] == "needs_review"
    assert job["outcome"]["channel"] == "csm_review"
    assert job["outcome"]["requiresReview"] is True
    assert [s["key"] for s in job["stages"]] == [
        "signal", "context", "action", "content", "review", "delivery", "learning"
    ]


def test_cost_and_tokens_accrued():
    siva = store.autopilot_for_manager("csm-svasireddy")
    _run(siva, _account("ACC-1002"))  # Nordia
    job = store.jobs()[0]
    s = job["stats"]
    assert s["turns"] >= 1
    assert s["total_tokens"] > 0
    # Token-based cost: priced models report a float, unpriced report None (never fabricated).
    if s["priced"]:
        assert s["cost_usd"] is not None and s["cost_usd"] >= 0.0
    else:
        assert s["cost_usd"] is None


def test_job_carries_manager_and_account():
    cora = store.autopilot_for_manager("csm-cora-thomas")
    _run(cora, _account("ACC-1006"))  # Tokyo Maritime Trust
    job = store.jobs()[0]
    assert job["manager"]["id"] == "csm-cora-thomas"
    assert job["account"]["account_id"] == "ACC-1006"


# ── scoping ────────────────────────────────────────────────────────
def test_manager_scoped_metrics_isolated():
    _run(store.autopilot_for_manager("csm-svasireddy"), _account("ACC-1001"))
    _run(store.autopilot_for_manager("csm-cora-thomas"), _account("ACC-1005"))

    siva = store.metrics("csm-svasireddy")
    assert siva["scope"] == "csm-svasireddy"
    assert siva["kpis"]["fleetSize"] == 1
    assert siva["kpis"]["jobsTotal"] == 1
    assert siva["kpis"]["accountsCovered"] == len(data_store.find("accounts", csm_manager_id="csm-svasireddy"))

    cora_jobs = store.jobs(manager_id="csm-cora-thomas")
    assert len(cora_jobs) == 1
    assert cora_jobs[0]["account"]["account_id"] == "ACC-1005"


def test_manager_review_queue_isolated():
    _run(store.autopilot_for_manager("csm-svasireddy"), _account("ACC-1001"))
    siva_q = store.review_queue("csm-svasireddy")
    assert any(not i.get("seeded") for i in siva_q)
    assert all(i.get("manager_id") in (None, "csm-svasireddy") for i in siva_q)
    # Mario has not run; his live queue should hold no Siva items.
    mario_q = store.review_queue("csm-mario-rogers")
    assert all(i.get("manager_id") != "csm-svasireddy" for i in mario_q)


# ── sponsor overview ───────────────────────────────────────────────
def test_sponsor_overview_one_row_per_autopilot():
    _run(store.autopilot_for_manager("csm-svasireddy"), _account("ACC-1001"))
    overview = store.sponsor_overview()
    assert overview["owner"]["name"] == "Siva Vasireddy"
    assert len(overview["rows"]) == 3
    siva_row = next(r for r in overview["rows"] if r["manager"]["id"] == "csm-svasireddy")
    assert siva_row["accountsCovered"] == len(data_store.find("accounts", csm_manager_id="csm-svasireddy"))
    assert siva_row["jobs"] == 1
    assert siva_row["queueLength"] >= 1
    assert overview["totals"]["autopilots"] == 3
    assert overview["totals"]["arrCovered"] > 0


# ── review loop + evidence ─────────────────────────────────────────
def test_review_decision_updates_outcome():
    _run(store.autopilot_for_manager("csm-svasireddy"), _account("ACC-1001"))
    queue = store.review_queue()
    live = [o for o in queue if not o.get("seeded")]
    assert live, "expected at least one live review item"
    updated = store.decide_review(live[0]["id"], "accept")
    assert updated["reviewDecision"] == "accept"
    assert updated["delivered"] is True
    assert updated["status"] == "delivered"


def test_evidence_fields_present():
    _run(store.autopilot_for_manager("csm-mario-rogers"), _account("ACC-1009"))
    job = store.jobs()[0]
    assert job["stats"]["model"]
    assert job["toolData"]
    assert job["outcome"] is not None
