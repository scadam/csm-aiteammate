#!/usr/bin/env python3
"""Render the BYO MCP registration file from environment configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "scripts" / "byo-mcp-registration.template.json"
OUTPUT = ROOT / "scripts" / "byo-mcp-registration.json"


def render() -> Path:
    payload = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    public_url = os.getenv("MCP__PUBLIC_URL", "")
    remote_scope = os.getenv("A365__TOOLING_GATEWAY__REMOTE_SCOPE", "")
    if not public_url.startswith("https://"):
        raise ValueError("MCP__PUBLIC_URL must be a public HTTPS URL")
    if not remote_scope.startswith("api://"):
        raise ValueError("A365__TOOLING_GATEWAY__REMOTE_SCOPE must be an api:// scope")
    payload["serverUrl"] = public_url.rstrip("/") + "/mcp"
    payload["remoteScopes"] = remote_scope
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(render())
