#!/usr/bin/env python3
"""Run resumable Agent 365 CLI setup without persisting or printing secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".a365"
STATE_FILE = STATE_DIR / "provisioning-state.json"
SAFE_ENV_FILE = STATE_DIR / "runtime-values.env"
TEAMS_ENV_FILE = ROOT / "env" / ".env.dev"
_ADMIN_URL = re.compile(r"https://login\.microsoftonline\.com/[^\s]+adminconsent[^\s]*", re.I)
_SECRET_LINE = re.compile(
    r"(?i)(client.?secret|secret.?text|access.?token|refresh.?token|password)\s*[:=].*"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_spec() -> dict[str, Any]:
    payload = yaml.safe_load((ROOT / "solution.yaml").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("solution.yaml must contain an object")
    return payload


def _state() -> dict[str, Any]:
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"schemaVersion": 1, "steps": {}, "safeOutputs": {}, "updatedAt": _now()}


def _save(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updatedAt"] = _now()
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _sanitize(line: str) -> str:
    if _SECRET_LINE.search(line):
        key = line.split(":", 1)[0].split("=", 1)[0]
        return f"{key}: [REDACTED]"
    return line.rstrip()


def _run(step: str, command: list[str], state: dict[str, Any]) -> str:
    print(f"\n[{step}] {' '.join(command)}", flush=True)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    assert process.stdout is not None
    for raw in process.stdout:
        clean = _sanitize(raw)
        lines.append(clean)
        print(clean, flush=True)
    return_code = process.wait()
    output = "\n".join(lines)
    consent_urls = sorted(set(_ADMIN_URL.findall(output)))
    if consent_urls:
        status = "admin_action_required"
    elif return_code == 0:
        status = "complete"
    else:
        status = "failed"
    state["steps"][step] = {
        "status": status,
        "exitCode": return_code,
        "adminConsentUrls": consent_urls,
        "updatedAt": _now(),
    }
    _capture_safe_outputs(state)
    _save(state)
    if return_code != 0 and not consent_urls:
        raise RuntimeError(f"A365 step {step} failed with exit code {return_code}")
    return status


def _latest_generated_config() -> Path | None:
    candidates = sorted(
        ROOT.glob("a365.generated.config*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _capture_safe_outputs(state: dict[str, Any]) -> None:
    source = _latest_generated_config()
    if source is None:
        return
    payload = json.loads(source.read_text(encoding="utf-8"))
    safe_keys = (
        "agentBlueprintId",
        "agentRegistrationId",
        "agentBlueprintObjectId",
        "agentBlueprintServicePrincipalObjectId",
        "agenticAppId",
        "cliVersion",
        "completed",
    )
    safe = {key: payload.get(key) for key in safe_keys if payload.get(key) is not None}
    safe["sourceFile"] = source.name
    safe["resourceConsents"] = [
        {
            "resourceName": item.get("resourceName"),
            "resourceAppId": item.get("resourceAppId"),
            "consentGranted": bool(item.get("consentGranted")),
            "scopes": item.get("scopes", []),
            "inheritablePermissionsConfigured": bool(
                item.get("inheritablePermissionsConfigured")
            ),
        }
        for item in payload.get("resourceConsents", [])
    ]
    state["safeOutputs"] = safe
    blueprint_id = safe.get("agentBlueprintId")
    values = []
    if blueprint_id:
        values.extend(
            [
                f"A365_BLUEPRINT_APP_ID={blueprint_id}",
                f"AGENT__IDENTITY__BLUEPRINT_ID={blueprint_id}",
            ]
        )
        _update_teams_environment(blueprint_id=blueprint_id)
    SAFE_ENV_FILE.write_text("\n".join(values) + ("\n" if values else ""), encoding="utf-8")


def _update_teams_environment(
    *, blueprint_id: str, control_plane_url: str = ""
) -> None:
    values = {"A365_BLUEPRINT_APP_ID": blueprint_id}
    if control_plane_url:
        parsed = urlsplit(control_plane_url.rstrip("/"))
        domain = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not domain or parsed.path not in {"", "/"}:
            raise ValueError("Control-plane URL must be an HTTPS origin without a path")
        values["CONTROL_PLANE_DOMAIN"] = domain
    TEAMS_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = TEAMS_ENV_FILE.read_text(encoding="utf-8").splitlines() if TEAMS_ENV_FILE.is_file() else []
    found: set[str] = set()
    rendered: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            rendered.append(f"{key}={values[key]}")
            found.add(key)
        else:
            rendered.append(line)
    rendered.extend(f"{key}={value}" for key, value in values.items() if key not in found)
    TEAMS_ENV_FILE.write_text("\n".join(rendered) + "\n", encoding="utf-8")


def _base_command(agent_name: str, tenant_id: str) -> list[str]:
    command = ["--agent-name", agent_name]
    if tenant_id:
        command.extend(["--tenant-id", tenant_id])
    return command


def early(args: argparse.Namespace) -> int:
    if shutil.which("a365") is None:
        raise RuntimeError(
            "A365 CLI is not installed. Run: dotnet tool install -g Microsoft.Agents.A365.DevTools.Cli"
        )
    spec = _load_spec()
    agent_name = args.agent_name or spec["solution"]["name"]
    common = _base_command(agent_name, args.tenant_id)
    state = _state()
    _run("requirements", ["a365", "setup", "requirements"], state)
    blueprint = ["a365", "setup", "blueprint", *common, "--no-endpoint", "--m365"]
    permissions_mcp = ["a365", "setup", "permissions", "mcp", *common]
    permissions_bot = ["a365", "setup", "permissions", "bot", *common]
    if args.dry_run:
        blueprint.append("--dry-run")
        permissions_mcp.append("--dry-run")
        permissions_bot.append("--dry-run")
    _run("blueprint", blueprint, state)
    _run("permissions_mcp", permissions_mcp, state)
    _run("permissions_bot", permissions_bot, state)
    pending = [
        name
        for name, result in state["steps"].items()
        if result["status"] == "admin_action_required"
    ]
    if pending:
        print(
            "\nA365 bootstrap created the available resources, but administrator consent "
            f"is still required for: {', '.join(pending)}. The URLs are in {STATE_FILE}.",
            flush=True,
        )
        return 2
    print(f"\nA365 early provisioning complete. Safe state: {STATE_FILE}", flush=True)
    return 0


def verify(args: argparse.Namespace) -> int:
    spec = _load_spec()
    agent_name = args.agent_name or spec["solution"]["name"]
    common = _base_command(agent_name, args.tenant_id)
    state = _state()
    _run("verify_blueprint_scopes", ["a365", "query-entra", "blueprint-scopes", *common], state)
    _run("verify_inheritance", ["a365", "query-entra", "inheritance", *common], state)
    if args.instance:
        _run("verify_instance_scopes", ["a365", "query-entra", "instance-scopes", *common], state)
    return 0


def endpoint(args: argparse.Namespace) -> int:
    parsed = urlsplit(args.url.rstrip("/"))
    if parsed.scheme != "https" or not parsed.hostname or parsed.path != "/api/messages":
        raise ValueError("Messaging endpoint must be a public HTTPS /api/messages URL")
    spec = _load_spec()
    agent_name = args.agent_name or spec["solution"]["name"]
    common = _base_command(agent_name, args.tenant_id)
    state = _state()
    command = [
        "a365",
        "setup",
        "blueprint",
        *common,
        "--update-endpoint",
        args.url,
    ]
    if args.dry_run:
        command.append("--dry-run")
    status = _run("messaging_endpoint", command, state)
    if status == "complete":
        state["safeOutputs"]["sharedPublicOrigin"] = f"https://{parsed.netloc}"
        _save(state)
    return 0


def publish(args: argparse.Namespace) -> int:
    if not args.control_plane_url.startswith("https://"):
        raise ValueError("Control-plane URL must be public HTTPS")
    state = _state()
    blueprint_id = state.get("safeOutputs", {}).get("agentBlueprintId")
    if not blueprint_id:
        raise RuntimeError("Run early provisioning before publishing")
    shared_origin = state.get("safeOutputs", {}).get("sharedPublicOrigin", "")
    if not shared_origin:
        raise RuntimeError("Register the shared messaging endpoint before publishing Teams")
    if args.control_plane_url.rstrip("/") != shared_origin:
        raise ValueError(
            "Control-plane URL must use the same public origin as the A365 messaging endpoint"
        )
    environment = os.environ.copy()
    environment["A365_BLUEPRINT_APP_ID"] = blueprint_id
    environment["CONTROL_PLANE_PUBLIC_URL"] = args.control_plane_url.rstrip("/")
    _update_teams_environment(
        blueprint_id=blueprint_id,
        control_plane_url=args.control_plane_url,
    )
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "render_agent365_manifest.py")],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    spec = _load_spec()
    agent_name = args.agent_name or spec["solution"]["name"]
    common = _base_command(agent_name, args.tenant_id)
    command = ["a365", "publish", *common, "--aiteammate", "true"]
    if args.dry_run:
        command.append("--dry-run")
    _run("publish", command, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-name", default="")
    parser.add_argument("--tenant-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("early")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--instance", action="store_true")
    endpoint_parser = subparsers.add_parser("endpoint")
    endpoint_parser.add_argument("--url", required=True)
    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--control-plane-url", required=True)
    args = parser.parse_args()
    return {
        "early": early,
        "verify": verify,
        "endpoint": endpoint,
        "publish": publish,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
