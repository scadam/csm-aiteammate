"""Tests that the tool registry exposes both Copilot and MCP surfaces."""

import asyncio


def test_registry_has_all_capabilities():
    from src.tools import COPILOT_TOOLS, TOOL_SPECS

    names = {spec.name for spec in TOOL_SPECS}
    expected = {
        "query_csm_database",
        "get_schema",
        "write_outcome",
        "search_knowledge_base",
        "get_account_context",
        "create_review_task",
        "send_email",
        "trigger_in_product_message",
        "get_engagement_history",
        "build_draft",
        "detect_signals",
        "decide_next_best_action",
        "search_microsoft_365",
        "ask",
        "list_agents",
        "gainsight_rest",
    }
    assert expected <= names
    assert len(COPILOT_TOOLS) == len(TOOL_SPECS)


def test_mcp_server_builds_all_tools():
    from src.mcp.server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert len(tools) == 19
    names = {t.name for t in tools}
    assert {"get_skill", "remember", "recall"} <= names
    schema = next(t.inputSchema for t in tools if t.name == "search_knowledge_base")
    assert "store" in schema.get("properties", {})
