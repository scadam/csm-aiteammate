#!/usr/bin/env python3
"""Render the tabs-only Teams host from safe A365/deployment outputs."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "appPackage" / "manifest.json"
OUTPUT = ROOT / "appPackage" / "build" / "manifest.json"
_GUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


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


def render(*, teams_app_id: str, blueprint_app_id: str, control_plane_url: str) -> Path:
    if not _GUID.fullmatch(teams_app_id):
        raise ValueError("teams_app_id must be a real GUID created by teamsApp/create")
    if not _GUID.fullmatch(blueprint_app_id):
        raise ValueError("blueprint_app_id must be a real A365 CLI blueprint GUID")
    parsed = urlsplit(control_plane_url.rstrip("/"))
    domain = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not domain or parsed.path not in {"", "/"}:
        raise ValueError("control_plane_url must be an HTTPS origin without a path")
    if not _DOMAIN.fullmatch(domain) or domain in {"localhost", "127.0.0.1"}:
        raise ValueError("control_plane_url must use a public lowercase DNS name")
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    rendered = _replace(
        payload,
        {
            "${{TEAMS_APP_ID}}": teams_app_id,
            "${{A365_BLUEPRINT_APP_ID}}": blueprint_app_id,
            "${{CONTROL_PLANE_DOMAIN}}": domain,
        },
    )
    expected_resource = f"api://{domain}/{blueprint_app_id}"
    if rendered.get("webApplicationInfo", {}).get("resource") != expected_resource:
        raise ValueError("Rendered Teams SSO audience does not match the control-plane origin")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(rendered, indent=2) + "\n", encoding="utf-8")
    for icon in ("color.png", "outline.png"):
        (OUTPUT.parent / icon).write_bytes((SOURCE.parent / icon).read_bytes())
    return OUTPUT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teams-app-id", required=True)
    parser.add_argument("--blueprint-app-id", required=True)
    parser.add_argument("--control-plane-url", required=True)
    args = parser.parse_args()
    print(
        render(
            teams_app_id=args.teams_app_id,
            blueprint_app_id=args.blueprint_app_id,
            control_plane_url=args.control_plane_url,
        )
    )


if __name__ == "__main__":
    main()
