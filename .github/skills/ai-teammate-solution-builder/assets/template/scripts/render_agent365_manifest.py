#!/usr/bin/env python3
"""Render Agent 365 manifest templates from real A365 CLI output IDs."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest" / "manifest.json"
TEMPLATE = ROOT / "manifest" / "agenticUserTemplateManifest.json"
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


def _guid(name: str, *, generate: bool = False) -> str:
    value = os.getenv(name, "")
    if not value and generate:
        value = str(uuid.uuid4())
    if not _GUID.fullmatch(value):
        raise ValueError(f"{name} must be a real GUID")
    return value


def _replace(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for token, replacement in replacements.items():
            value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def render() -> tuple[Path, Path]:
    public_url = os.getenv("CONTROL_PLANE_PUBLIC_URL", "").rstrip("/")
    if not public_url.startswith("https://"):
        raise ValueError("CONTROL_PLANE_PUBLIC_URL must be a public HTTPS URL")
    replacements = {
        "${A365_BLUEPRINT_APP_ID}": _guid("A365_BLUEPRINT_APP_ID"),
        "${A365_AGENTIC_TEMPLATE_ID}": _guid("A365_AGENTIC_TEMPLATE_ID", generate=True),
        "${CONTROL_PLANE_PUBLIC_URL}": public_url,
    }
    for path in (MANIFEST, TEMPLATE):
        payload = json.loads(path.read_text(encoding="utf-8"))
        rendered = _replace(payload, replacements)
        path.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")
    return MANIFEST, TEMPLATE


if __name__ == "__main__":
    for item in render():
        print(item)
