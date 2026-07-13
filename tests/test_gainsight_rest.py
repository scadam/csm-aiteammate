"""Tests for the simulated-real Gainsight NXT REST API."""

import pytest

from src.gainsight.client import get_client


@pytest.fixture()
def gs():
    return get_client()


def test_company_read_envelope(gs):
    r = gs.request("POST", "/v1/data/objects/query/Company", {
        "select": ["Name", "Arr", "Sentiment__gc"],
        "where": {"conditions": [
            {"name": "Sentiment__gc", "alias": "A", "value": ["Frustrated"], "operator": "EQ"}
        ], "expression": "A"},
    })
    assert r["result"] is True
    assert "requestId" in r
    assert any(row["Name"] == "Meridian Capital Partners" for row in r["data"])


def test_company_read_requires_select(gs):
    r = gs.request("POST", "/v1/data/objects/query/Company", {})
    assert r["result"] is False
    assert r["errorCode"] == "GSOBJ_1001"


def test_cta_create_and_fetch(gs):
    created = gs.request("POST", "/v2/cockpit/cta", {"requests": [{"record": {
        "referenceId": "T-1", "Name": "Test", "AccountId__gc": "ACC-1002",
        "type": "feature_tip", "reason": "email", "status": "New", "priority": "Low",
        "Comments": "hi"}}]})
    assert created["result"] is True
    assert created["data"]["success"]
    gsid = next(iter(created["data"]["success"][0].values()))
    assert gsid.startswith("1S01")

    fetched = gs.request("POST", "/v2/cockpit/cta/list", {
        "select": ["Name", "StatusId__gr.Name"],
        "where": {"conditions": [
            {"name": "IsClosed", "alias": "A", "value": ["false"], "operator": "EQ"}
        ], "expression": "A"},
        "pageSize": 50, "pageNumber": 1,
    })
    assert fetched["result"] is True
    assert isinstance(fetched["data"], list)


def test_cta_config(gs):
    cfg = gs.request("GET", "/v2/cockpit/admin/picklist/lite",
                     params={"category": "CTA_STATUS,CTA_TYPE", "et": "COMPANY"})
    assert cfg["result"] is True
    assert "CTA_STATUS" in cfg["data"] and "CTA_TYPE" in cfg["data"]


def test_px_engagements(gs):
    px = gs.request("GET", "/v1/engagements")
    assert "engagements" in px
    assert px["totalElements"] >= 1


def test_unknown_endpoint(gs):
    r = gs.request("POST", "/v1/does/not/exist", {})
    assert r["result"] is False
    assert r["errorCode"] == "GS_4040"
