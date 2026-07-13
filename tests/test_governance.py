"""Tests for the SIT scanner, agent memory, skills loader, and Purview ledger."""

import asyncio

import pytest

from src import memory, purview, sit, skills


# ── SIT scanner ────────────────────────────────────────────────────
def test_sit_detects_high_confidence_types():
    text = "Card 4532 6677 8521 3500, SSN 120-98-1437, email a@b.com"
    found = {m.sit: m for m in sit.detect(text)}
    assert "Credit Card Number" in found
    assert found["Credit Card Number"].confidence == "High"
    assert "U.S. Social Security Number (SSN)" in found
    assert "Email Address" in found


def test_sit_credit_card_requires_luhn():
    # 4111 1111 1111 1111 is a valid Luhn test card; a random 16-digit is not.
    assert any(m.sit == "Credit Card Number" for m in sit.detect("4111111111111111"))
    assert not any(m.sit == "Credit Card Number" for m in sit.detect("1234567812345678"))


def test_classify_label_escalates_with_high_sits():
    assert sit.classify("nothing sensitive here") == sit.LABEL_GENERAL
    assert sit.classify("ssn 120-98-1437") == sit.LABEL_HIGHLY_CONFIDENTIAL


# ── skills ─────────────────────────────────────────────────────────
def test_skills_load_from_folders():
    names = {s.name for s in skills.list_skills()}
    assert {"adoption-recovery", "renewal-risk-brief", "voice-matched-outreach",
            "enhancement-match", "escalation-triage"} <= names


def test_skill_has_description_and_body():
    s = next(s for s in skills.list_skills() if s.name == "renewal-risk-brief")
    assert s.description
    body = skills.load_skill("renewal-risk-brief")
    assert body and "risk brief" in body.lower()


def test_skills_declare_allowed_tools():
    # Every skill restricts itself to a real subset of the tool registry (to-do J).
    from src.tools import TOOL_SPECS
    registry = {t.name for t in TOOL_SPECS}
    for s in skills.list_skills():
        assert s.allowed_tools, f"{s.name} has no allowed-tools"
        assert set(s.allowed_tools) <= registry, f"{s.name} references unknown tools"


def test_catalog_markdown_lists_skills():
    cat = skills.catalog_markdown()
    assert "adoption-recovery" in cat and "get_skill" in cat


# ── memory ─────────────────────────────────────────────────────────
def test_memory_append_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(memory.config, "MEMORY_DIR", tmp_path)
    text = memory.append_learning("csm-test", "What worked", "Lead with value, keep the ask tiny.", "Test CSM")
    assert "Lead with value" in text
    assert "(none yet)" not in text.split("## What worked")[1].split("##")[0]
    # A second learning accumulates under the same section.
    text2 = memory.append_learning("csm-test", "What worked", "Reference prior VOC to personalise.")
    assert "Reference prior VOC" in text2 and "Lead with value" in text2


def test_memory_creates_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(memory.config, "MEMORY_DIR", tmp_path)
    text = memory.load("csm-new", "New CSM")
    for section in ("Insights", "What worked", "What to avoid", "Account notes"):
        assert f"## {section}" in text


# ── Purview ledger + governance ────────────────────────────────────
def test_purview_tag_data_records_confidential():
    purview.reset()
    asyncio.run(purview.tag_data(source="Snowflake (CSM_DB)",
                                 manager={"id": "csm-test", "name": "Test", "entra_object_id": None},
                                 account_id="ACC-1001", summary="health 48, ssn 120-98-1437",
                                 label=sit.LABEL_CONFIDENTIAL))
    led = purview.ledger()
    assert led and led[0]["label"] == sit.LABEL_CONFIDENTIAL
    assert led[0]["source"] == "Snowflake (CSM_DB)"
    assert any(t["sit"] == "U.S. Social Security Number (SSN)" for t in led[0]["sits"])


def test_purview_process_content_offline_allows_and_records():
    purview.reset()
    decision = asyncio.run(purview.process_content(
        text="Hi Kai, CheckMate refresh is live.", activity="downloadText",
        manager={"id": "csm-test", "name": "Test", "entra_object_id": None},
        correlation_id="job-1", sequence=1, source="response", account_id="ACC-1001"))
    assert decision.allowed is True
    assert decision.real is False  # no OBO token offline
    summary = purview.governance_summary()
    assert summary["totals"]["responses"] == 1


def test_purview_log_tool_call_records_dspm_event():
    purview.reset()
    asyncio.run(purview.log_tool_call(
        tool="query_csm_database",
        manager={"id": "csm-test", "name": "Test", "entra_object_id": None},
        arguments={"question": "low adoption accounts"},
        result="3 rows: ACC-1001, ACC-1002, ACC-1003", surface="MCP tool",
        correlation_id="job-1", account_id="ACC-1001"))
    led = purview.ledger()
    assert led, "tool call should be recorded on the ledger"
    row = led[0]
    assert row["activity"] == "toolCall"
    assert row["activityLabel"] == "Tool call"
    assert row["tool"] == "query_csm_database"
    assert row["source"] == "MCP tool: query_csm_database"
    assert row["real"] is False  # no OBO/app token offline
    # governance_summary counts tool calls in their own bucket (not data-access)
    totals = purview.governance_summary()["totals"]
    assert totals["toolCalls"] == 1
    assert totals["dataAccessEvents"] == 0


def test_purview_log_tool_call_respects_disable_flag(monkeypatch):
    purview.reset()
    monkeypatch.setattr(purview.config, "PURVIEW_LOG_TOOL_CALLS", False)
    out = asyncio.run(purview.log_tool_call(
        tool="detect_signals", manager={"id": "csm-test", "entra_object_id": None},
        arguments={}, result="x"))
    assert out == {}
    assert purview.ledger() == []


def test_purview_status_reports_mode():
    st = purview.status()
    assert "Content.Process.User" in st["delegatedPermissions"]
    assert "Content.Process.All" in st["applicationPermissions"]
    assert st["mode"] in ("graph-app-only", "local-ledger")
