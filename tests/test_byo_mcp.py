"""Tests for the BYO MCP registration document + CLI command builder."""

from src.mcp import gateway


def test_registration_document_shape():
    doc = gateway.build_registration_document()
    assert doc["serverName"].startswith("ext_")
    assert len(doc["serverName"]) <= 20
    assert doc["authType"] == "EntraOAuth"
    assert isinstance(doc["tools"], list) and doc["tools"]
    # every declared tool has a name + description
    assert all("name" in t and "description" in t for t in doc["tools"])
    # the combined server exposes Snowflake + Gainsight + CSM tools
    names = {t["name"] for t in doc["tools"]}
    assert {"query_csm_database", "gainsight_rest", "get_account_context"} <= names


def test_cli_command_uses_register_external():
    cmd = gateway.build_cli_command()
    assert cmd[:3] == ["a365", "develop-mcp", "register-external-mcp-server"]
    assert "--auth-type" in cmd and "EntraOAuth" in cmd


def test_cli_command_with_file():
    cmd = gateway.build_cli_command("byo.json")
    assert cmd == ["a365", "develop-mcp", "register-external-mcp-server", "-f", "byo.json"]
