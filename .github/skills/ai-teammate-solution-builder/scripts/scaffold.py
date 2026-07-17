#!/usr/bin/env python3
"""Create a spec-driven AI teammate ASGI solution from the bundled template."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import yaml

SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from studio.core import consume_confirmation
from studio.grants import GrantLedger
from studio.validation import confined_source, load_spec, validate_spec

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover - tooling prerequisite
    raise SystemExit("Install the scaffold prerequisites: pip install pyyaml jsonschema") from exc


ASSETS = SKILL_ROOT / "assets"
TEMPLATE = ASSETS / "template"
SCHEMA = ASSETS / "solution.schema.json"
MARKER = ".generated-ai-teammate"


def _load(path: Path) -> dict[str, Any]:
    return load_spec(path)


def _validate(spec: dict[str, Any], spec_path: Path | None = None) -> None:
    validate_spec(spec, spec_path, schema_path=SCHEMA)


def _confirmation_sidecars(spec: dict[str, Any], spec_path: Path) -> dict[str, str]:
    paths: set[str] = set()
    for source in spec.get("openapi_sources", []):
        if source.get("document"):
            paths.add(source["document"])
    for source in spec.get("data_sources", []):
        if source.get("kind") == "json" and source.get("path"):
            paths.add(source["path"])
    result: dict[str, str] = {}
    for relative in sorted(paths):
        target = _confined_source(spec_path.parent, relative)
        result[relative] = (
            __import__("hashlib").sha256(target.read_bytes()).hexdigest()
            if target.is_file()
            else "missing"
        )
    return result


def _require_unique(kind: str, values: list[str]) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {kind} id.")


def _reject_credential_fields(capability_id: str, schema: dict[str, Any]) -> None:
    forbidden = {
        "authorization", "token", "access_token", "api_key", "apikey",
        "password", "secret", "client_secret", "cookie", "host", "url", "method",
    }
    for name, child in schema.get("properties", {}).items():
        normalized = name.lower().replace("-", "_")
        if normalized in forbidden:
            raise ValueError(
                f"Capability {capability_id} exposes forbidden credential/transport field {name!r}."
            )
        if isinstance(child, dict) and child.get("type") == "object":
            _reject_credential_fields(capability_id, child)


def _validate_openapi_documents(spec: dict[str, Any], spec_path: Path) -> None:
    for source in spec["openapi_sources"]:
        document_path = _confined_source(spec_path.parent, source["document"])
        if not document_path.is_file():
            raise ValueError(f"OpenAPI source {source['id']} does not exist: {document_path}")
        document = _load(document_path)
        if not str(document.get("openapi", "")).startswith("3."):
            raise ValueError(f"OpenAPI source {source['id']} must use OpenAPI 3.x.")
        paths = document.get("paths", {})
        for operation in source["operations"]:
            path_item = paths.get(operation["path"], {})
            actual = path_item.get(operation["method"].lower())
            if not isinstance(actual, dict) or actual.get("operationId") != operation["operation_id"]:
                raise ValueError(
                    f"OpenAPI operation {source['id']}.{operation['operation_id']} "
                    "does not match the declared method/path."
                )
            _validate_openapi_input_schema(
                source["id"], operation, path_item, actual
            )


def _validate_openapi_input_schema(
    source_id: str,
    operation: dict[str, Any],
    path_item: dict[str, Any],
    actual: dict[str, Any],
) -> None:
    declared = operation["input_schema"]
    properties = declared.get("properties", {})
    declared_required = set(declared.get("required", []))
    parameters = [*path_item.get("parameters", []), *actual.get("parameters", [])]
    expected: dict[str, tuple[str, bool, dict[str, Any]]] = {}
    for parameter in parameters:
        if "$ref" in parameter:
            raise ValueError(
                f"OpenAPI operation {source_id}.{operation['operation_id']} uses an unsupported parameter $ref."
            )
        location = parameter.get("in")
        if location not in {"path", "query"}:
            raise ValueError(
                f"OpenAPI operation {source_id}.{operation['operation_id']} uses unsupported parameter location {location!r}."
            )
        expected[parameter["name"]] = (
            location,
            bool(parameter.get("required")),
            parameter.get("schema", {}),
        )
    request_body = actual.get("requestBody")
    if request_body:
        content = request_body.get("content", {})
        body_schema = content.get("application/json", {}).get("schema")
        if not isinstance(body_schema, dict):
            raise ValueError(
                f"OpenAPI operation {source_id}.{operation['operation_id']} must use an inline application/json body."
            )
        expected["body"] = ("body", bool(request_body.get("required")), body_schema)
    if set(properties) != set(expected):
        raise ValueError(
            f"OpenAPI operation {source_id}.{operation['operation_id']} input properties "
            f"must be exactly {sorted(expected)}."
        )
    for name, (location, required, source_schema) in expected.items():
        declared_schema = properties[name]
        if declared_schema.get("x-in", "query") != location:
            raise ValueError(
                f"OpenAPI input {source_id}.{operation['operation_id']}.{name} has the wrong x-in location."
            )
        if required != (name in declared_required):
            raise ValueError(
                f"OpenAPI input {source_id}.{operation['operation_id']}.{name} has the wrong required policy."
            )
        _compare_schema_shape(
            f"{source_id}.{operation['operation_id']}.{name}", declared_schema, source_schema
        )


def _compare_schema_shape(name: str, declared: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("type", "format", "enum"):
        if key in source and declared.get(key) != source.get(key):
            raise ValueError(f"OpenAPI input {name} disagrees on {key}.")
    if source.get("type") == "object":
        if set(declared.get("properties", {})) != set(source.get("properties", {})):
            raise ValueError(f"OpenAPI input {name} has different object properties.")
        if set(declared.get("required", [])) != set(source.get("required", [])):
            raise ValueError(f"OpenAPI input {name} has different required properties.")


def _project_name(solution_id: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", solution_id.lower().replace("_", "-")).strip("-")


def _prepare_output(output: Path, force: bool) -> None:
    if not output.exists():
        return
    if not any(output.iterdir()):
        return
    if not force:
        raise FileExistsError(f"Output directory is not empty: {output}")
    if not (output / MARKER).exists():
        raise FileExistsError(
            f"Refusing to replace {output}: it is not marked as a generated solution."
        )
    shutil.rmtree(output)


def scaffold(spec_path: Path, output: Path, force: bool = False) -> Path:
    spec_path = spec_path.resolve()
    output = output.resolve()
    spec = _load(spec_path)
    _validate(spec, spec_path)
    _prepare_output(output, force)

    shutil.copytree(TEMPLATE, output, dirs_exist_ok=True)
    (output / "openapi").mkdir(parents=True, exist_ok=True)
    (output / "data").mkdir(parents=True, exist_ok=True)
    tokens = {
        "__PROJECT_ID__": _project_name(spec["solution"]["id"]),
        "__PROJECT_NAME__": spec["solution"]["name"],
        "__PROJECT_DESCRIPTION__": spec["solution"]["description"],
        "__AGENT_PORT_ENV__": spec["runtime"]["agent_host"]["port_env"],
        "__CONTROL_PLANE_PORT_ENV__": spec["runtime"]["control_plane"]["port_env"],
        "__MCP_PORT_ENV__": spec["runtime"]["mcp_host"]["port_env"],
        "__MODEL_ENV__": spec["agent"]["reasoning"]["model_env"],
        "__INTEGRATION_BICEP_PARAMS__": _integration_bicep_params(spec),
        "__INTEGRATION_BICEP_ENV__": _integration_bicep_env(spec),
        "__INTEGRATION_BICEP_SECRETS__": _integration_bicep_secrets(spec),
        "__INTEGRATION_ENV_TEMPLATE__": _integration_env_template(spec),
    }
    for source_name, target_name in (("pyproject.toml.tpl", "pyproject.toml"), ("README.md.tpl", "README.md")):
        source = output / source_name
        text = source.read_text(encoding="utf-8")
        for key, value in tokens.items():
            text = text.replace(key, value)
        (output / target_name).write_text(text, encoding="utf-8")
        source.unlink()
    for relative in ("env.TEMPLATE", "infra/main.bicep"):
        target = output / relative
        text = target.read_text(encoding="utf-8")
        for key, value in tokens.items():
            text = text.replace(key, value)
        target.write_text(text, encoding="utf-8")

    rendered_spec = yaml.safe_dump(spec, sort_keys=False, allow_unicode=False)
    (output / "solution.yaml").write_text(
        "# yaml-language-server: $schema=./solution.schema.json\n" + rendered_spec,
        encoding="utf-8",
    )
    (output / "app" / "solution.yaml").write_text(rendered_spec, encoding="utf-8")
    shutil.copy2(SCHEMA, output / "solution.schema.json")
    for source in spec["openapi_sources"]:
        source_path = _confined_source(spec_path.parent, source["document"])
        target_path = _confined_output(output, source["document"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
    for source in spec["data_sources"]:
        if source["kind"] != "json":
            continue
        source_path = _confined_source(spec_path.parent, source["path"])
        if not source_path.is_file():
            raise ValueError(f"JSON data source {source['id']} does not exist: {source_path}")
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError(f"JSON data source {source['id']} must contain an array of objects.")
        target_path = _confined_output(output, source["path"])
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
    _write_skills(spec, output)
    _write_agent365_assets(spec, output)
    _write_teams_app_assets(spec, output)
    (output / MARKER).write_text("Generated by ai-teammate-solution-builder.\n", encoding="utf-8")
    return output


def _integration_bindings(spec: dict[str, Any]) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for server in spec["mcp_servers"]:
        if server["endpoint_env"] not in seen:
            bindings.append({"env": server["endpoint_env"], "param": f"mcp{_pascal(server['id'])}Endpoint", "secret": False})
            seen.add(server["endpoint_env"])
        if server.get("token_env") and server["token_env"] not in seen:
            bindings.append({"env": server["token_env"], "param": f"mcp{_pascal(server['id'])}Token", "secret": True})
            seen.add(server["token_env"])
    for source in spec["openapi_sources"]:
        if source["base_url_env"] not in seen:
            bindings.append({"env": source["base_url_env"], "param": f"openapi{_pascal(source['id'])}BaseUrl", "secret": False})
            seen.add(source["base_url_env"])
        token_env = source.get("auth", {}).get("token_env")
        if token_env and token_env not in seen:
            bindings.append({"env": token_env, "param": f"openapi{_pascal(source['id'])}Token", "secret": True})
            seen.add(token_env)
        for key, suffix in (
            ("token_url_env", "TokenUrl"),
            ("client_id_env", "ClientId"),
            ("client_secret_env", "ClientSecret"),
        ):
            env_name = source.get("auth", {}).get(key)
            if env_name and env_name not in seen:
                bindings.append({
                    "env": env_name,
                    "param": f"openapi{_pascal(source['id'])}{suffix}",
                    "secret": key == "client_secret_env",
                })
                seen.add(env_name)
    return bindings


def _integration_bicep_params(spec: dict[str, Any]) -> str:
    lines = []
    for binding in _integration_bindings(spec):
        lines.extend(
            [
                f"@description('Runtime value for {binding['env']}.')",
                *( ["@secure()"] if binding["secret"] else [] ),
                f"param {binding['param']} string = ''",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _integration_bicep_env(spec: dict[str, Any]) -> str:
    return "\n".join(
        (
            f"  {{ name: '{binding['env']}', secretRef: '{_secret_name(binding['env'])}' }}"
            if binding["secret"]
            else f"  {{ name: '{binding['env']}', value: {binding['param']} }}"
        )
        for binding in _integration_bindings(spec)
    )


def _integration_bicep_secrets(spec: dict[str, Any]) -> str:
    secrets = [binding for binding in _integration_bindings(spec) if binding["secret"]]
    return "\n".join(
        f"        {{ name: '{_secret_name(binding['env'])}', value: {binding['param']} }}"
        for binding in secrets
    )


def _integration_env_template(spec: dict[str, Any]) -> str:
    return "\n".join(f"{binding['env']}=" for binding in _integration_bindings(spec))


def _pascal(value: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_-]+", value))


def _secret_name(env_name: str) -> str:
    value = re.sub(r"[^a-z0-9-]", "-", env_name.lower().replace("_", "-"))
    return value.strip("-")[:253]


def _confined_source(root: Path, relative: str) -> Path:
    return confined_source(root, relative)


def _confined_output(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Output path escapes the generated project: {relative}")
    return resolved


def _write_skills(spec: dict[str, Any], output: Path) -> None:
    for skill in spec["skills"]:
        target = output / "app" / "generated_skills" / skill["id"] / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        direct = {
            item["id"]
            for item in spec["capabilities"]
            if "agent" in item["expose"]
        }
        allowed_tools = [item for item in skill["capabilities"] if item in direct]
        allowed_tools.extend(f"start_{item}" for item in skill["workflows"])
        allowed = ", ".join(allowed_tools)
        text = (
            "---\n"
            f"name: {skill['id']}\n"
            f"description: {json.dumps(skill['description'])}\n"
            f"when_to_use: {json.dumps(skill['when_to_use'])}\n"
            f"allowed-tools: [{allowed}]\n"
            "---\n\n"
            f"# {skill['title']}\n\n{skill['instructions'].strip()}\n"
        )
        target.write_text(text, encoding="utf-8")


def _write_agent365_assets(spec: dict[str, Any], output: Path) -> None:
    solution = spec["solution"]
    blueprint_id = "${A365_BLUEPRINT_APP_ID}"
    template_id = "${A365_AGENTIC_TEMPLATE_ID}"
    manifest_dir = output / spec["a365"]["manifest_dir"]
    manifest_dir.mkdir(parents=True, exist_ok=True)
    short_name = solution["name"][:30]
    full_name = solution["name"][:100]
    manifest = {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/vdevPreview/MicrosoftTeams.schema.json",
        "id": blueprint_id,
        "name": {"short": short_name, "full": full_name},
        "description": {
            "short": solution["description"][:80],
            "full": solution["description"][:4000],
        },
        "icons": {"outline": "outline.png", "color": "color.png"},
        "accentColor": "#0B625B",
        "version": "1.0.0",
        "manifestVersion": "devPreview",
        "developer": {
            "name": solution["name"],
            "mpnId": "",
            "websiteUrl": "${CONTROL_PLANE_PUBLIC_URL}",
            "privacyUrl": "${CONTROL_PLANE_PUBLIC_URL}/privacy",
            "termsOfUseUrl": "${CONTROL_PLANE_PUBLIC_URL}/terms",
        },
        "agenticUserTemplates": [
            {"id": template_id, "file": "agenticUserTemplateManifest.json"}
        ],
    }
    template_manifest = {
        "id": template_id,
        "schemaVersion": "0.1.0-preview",
        "agentIdentityBlueprintId": blueprint_id,
        "communicationProtocol": "activityProtocol",
    }
    (manifest_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (manifest_dir / "agenticUserTemplateManifest.json").write_text(
        json.dumps(template_manifest, indent=2) + "\n", encoding="utf-8"
    )
    _write_png(manifest_dir / "color.png", 192, (11, 98, 91, 255))
    _write_png(manifest_dir / "outline.png", 32, (255, 255, 255, 255), transparent=True)

    a365_config = {
        "tenantId": "",
        "clientAppId": "",
        "agentIdentityDisplayName": f"{solution['name']} Identity",
        "agentBlueprintDisplayName": f"{solution['name']} Blueprint",
        "agentDescription": solution["description"],
        "aiTeammate": True,
        "useBlueprint": True,
    }
    (output / spec["a365"]["config_file"]).write_text(
        json.dumps(a365_config, indent=2) + "\n", encoding="utf-8"
    )
    tools = [
        {"name": item["id"], "description": item["description"]}
        for item in spec["capabilities"]
        if "mcp" in item["expose"]
    ]
    tools.append(
        {"name": "get_skill", "description": "Load scenario skill instructions."}
    )
    tools.extend(
        {
            "name": f"start_{workflow['id']}",
            "description": f"Start the policy-governed {workflow['title']} workflow.",
        }
        for workflow in spec["workflows"]
    )
    tools.extend(
        {
            "name": resource["tool_name"],
            "description": resource["description"],
        }
        for resource in spec["user_interfaces"]["resources"]
        if "mcp" in resource["surfaces"]
    )
    if any(
        resource["kind"] == "hitl" and "mcp" in resource["surfaces"]
        for resource in spec["user_interfaces"]["resources"]
    ):
        tools.append(
            {
                "name": "resolve_reviews",
                "description": "Resolve exact review effects selected in a generated HITL MCP App.",
            }
        )
    registration = {
        "serverName": spec["mcp_exposure"]["server_name"],
        "serverUrl": "${MCP__PUBLIC_URL}",
        "authType": "EntraOAuth",
        "description": f"Governed tools for {solution['name']}",
        "publisherName": solution["name"],
        "tools": tools,
        "remoteScopes": "${A365__TOOLING_GATEWAY__REMOTE_SCOPE}",
        "externalOAuth": None,
        "apiKey": None,
    }
    scripts = output / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "byo-mcp-registration.template.json").write_text(
        json.dumps(registration, indent=2) + "\n", encoding="utf-8"
    )


def _write_teams_app_assets(spec: dict[str, Any], output: Path) -> None:
        """Generate the tabs-only Teams host; A365 remains the identity owner."""
        solution = spec["solution"]
        teams = spec["teams_app"]
        package = output / "appPackage"
        package.mkdir(parents=True, exist_ok=True)
        tabs = [
                {
                        "entityId": tab["id"],
                        "name": tab["name"],
                        "contentUrl": f"https://${{{{CONTROL_PLANE_DOMAIN}}}}{tab['path']}",
                        "websiteUrl": f"https://${{{{CONTROL_PLANE_DOMAIN}}}}{tab['path']}",
                        "scopes": ["personal"],
                }
                for tab in teams["tabs"]
        ]
        manifest = {
                "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.26/MicrosoftTeams.schema.json",
                "manifestVersion": teams["manifest_version"],
                "version": "1.0.0",
                # A fresh catalog ID is created by teamsApp/create. It is deliberately
                # not the A365 blueprint/agent ID, avoiding inherited agent classification.
                "id": "${{TEAMS_APP_ID}}",
                "developer": {
                        "name": solution["name"],
                        "websiteUrl": "https://${{CONTROL_PLANE_DOMAIN}}",
                        "privacyUrl": "https://${{CONTROL_PLANE_DOMAIN}}/privacy",
                        "termsOfUseUrl": "https://${{CONTROL_PLANE_DOMAIN}}/terms",
                },
                "name": {
                        "short": solution["name"][:30],
                        "full": f"{solution['name']} control plane"[:100],
                },
                "description": {
                        "short": solution["description"][:80],
                        "full": solution["description"][:4000],
                },
                "icons": {"outline": "outline.png", "color": "color.png"},
                "accentColor": "#0B625B",
                "permissions": ["identity"],
                "staticTabs": tabs,
                "webApplicationInfo": {
                        "id": "${{A365_BLUEPRINT_APP_ID}}",
                        "resource": "api://${{CONTROL_PLANE_DOMAIN}}/${{A365_BLUEPRINT_APP_ID}}",
                },
                "defaultInstallScope": teams["default_install_scope"],
                "validDomains": ["${{CONTROL_PLANE_DOMAIN}}"],
        }
        (package / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        _write_png(package / "color.png", 192, (11, 98, 91, 255))
        _write_png(package / "outline.png", 32, (255, 255, 255, 255), transparent=True)

        env_dir = output / "env"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / ".env.dev").write_text(
                "# Safe package metadata. Fill from deployment/A365 outputs; never add secrets.\n"
                f"TEAMS_APP_NAME={solution['name']} Control Plane\n"
                "TEAMS_APP_ID=\n"
                "A365_BLUEPRINT_APP_ID=\n"
                "CONTROL_PLANE_DOMAIN=\n",
                encoding="utf-8",
        )
        package_name = teams["package_name"]
        package_path = f"./appPackage/build/{package_name}.${{{{TEAMSFX_ENV}}}}.zip"
        manifest_path = "./appPackage/build/manifest.json"
        render_action = {
            "uses": "script",
            "with": {
                "run": (
                    "python scripts/render_teams_manifest.py "
                    "--teams-app-id \"${{TEAMS_APP_ID}}\" "
                    "--blueprint-app-id \"${{A365_BLUEPRINT_APP_ID}}\" "
                    "--control-plane-url \"https://${{CONTROL_PLANE_DOMAIN}}\""
                )
            },
        }
        zip_action = {
            "uses": "teamsApp/zipAppPackage",
            "with": {
                "manifestPath": manifest_path,
                "outputZipPath": package_path,
                "outputFolder": "./appPackage/build",
            },
        }
        lifecycle = {
            "version": "v1.11",
            "environmentFolderPath": "./env",
            "provision": [
                {
                    "uses": "teamsApp/create",
                    "with": {"name": "${{TEAMS_APP_NAME}}"},
                    "writeToEnvironmentFile": {"teamsAppId": "TEAMS_APP_ID"},
                },
                render_action,
                {
                    "uses": "teamsApp/validateManifest",
                    "with": {"manifestPath": manifest_path},
                },
                zip_action,
                {
                    "uses": "teamsApp/update",
                    "with": {"appPackagePath": package_path},
                },
            ],
            "publish": [
                render_action,
                zip_action,
                {
                    "uses": "teamsApp/update",
                    "with": {"appPackagePath": package_path},
                },
                {
                    "uses": "teamsApp/publishAppPackage",
                    "with": {"appPackagePath": package_path},
                    "writeToEnvironmentFile": {
                        "publishedAppId": "TEAMS_APP_PUBLISHED_APP_ID"
                    },
                },
            ],
        }
        header = (
            "# yaml-language-server: $schema=https://aka.ms/m365-agents-toolkits/v1.11/yaml.schema.json\n"
            "# Creates only a fresh tabs-only catalog app. A365 owns Entra identity and consent.\n"
        )
        (output / "m365agents.yml").write_text(
            header + yaml.safe_dump(lifecycle, sort_keys=False), encoding="utf-8"
        )


def _write_png(
    path: Path, size: int, color: tuple[int, int, int, int], *, transparent: bool = False
) -> None:
    """Write a dependency-free valid RGBA PNG with a simple teammate mark."""
    rows: list[bytes] = []
    center = (size - 1) / 2
    radius = size * 0.29
    ring = max(1.5, size * 0.045)
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            distance = ((x - center) ** 2 + ((y - center) * 1.45) ** 2) ** 0.5
            on_ring = abs(distance - radius) <= ring
            on_core = ((x - center) ** 2 + (y - center) ** 2) ** 0.5 <= size * 0.09
            if transparent:
                pixel = color if on_ring or on_core else (0, 0, 0, 0)
            else:
                pixel = (255, 255, 255, 255) if on_ring or on_core else color
            row.extend(pixel)
        rows.append(bytes(row))
    raw = b"".join(rows)
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    path.write_bytes(signature + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="YAML or JSON solution spec")
    parser.add_argument("--output", type=Path, required=True, help="Destination directory")
    parser.add_argument(
        "--confirmation-file",
        type=Path,
        required=True,
        help="One-shot confirmation grant created by the reviewed Spec Studio draft",
    )
    parser.add_argument("--force", action="store_true", help="Replace a previously generated destination")
    parser.add_argument(
        "--provision-a365",
        action="store_true",
        help="Run early A365 requirements, blueprint, MCP, and bot permission setup",
    )
    parser.add_argument("--tenant-id", default="", help="Optional tenant override for A365 setup")
    parser.add_argument(
        "--a365-dry-run",
        action="store_true",
        help="Pass --dry-run to mutating A365 setup commands",
    )
    args = parser.parse_args()
    _validate(_load(args.spec), args.spec.resolve())
    grant = consume_confirmation(
        args.confirmation_file,
        spec_path=args.spec,
        output_path=args.output,
        force=args.force,
        provision_a365=args.provision_a365,
        tenant_id=args.tenant_id,
        sidecar_hashes=_confirmation_sidecars,
    )
    ledger = GrantLedger(args.confirmation_file.resolve().parent)
    try:
        result = scaffold(args.spec, args.output, args.force)
        print(f"Generated {result}")
        print(f"Next: cd {result}; python -m venv .venv; pip install -e '.[dev]'; pytest -q")
        if args.provision_a365:
            command = [sys.executable, str(result / "scripts" / "provision_agent365.py")]
            if args.tenant_id:
                command.extend(["--tenant-id", args.tenant_id])
            if args.a365_dry_run:
                command.append("--dry-run")
            command.append("early")
            completed = subprocess.run(command, cwd=result, check=False)
            if completed.returncode == 2:
                ledger.update_status(grant["grantId"], "checkpoint")
                print(
                    "A365 setup reached an administrator checkpoint. Resume from "
                    f"{result / '.a365' / 'provisioning-state.json'}"
                )
                raise SystemExit(2)
            if completed.returncode != 0:
                ledger.update_status(grant["grantId"], "failed")
                raise SystemExit(completed.returncode)
        ledger.update_status(grant["grantId"], "complete")
    except SystemExit:
        raise
    except Exception:
        current = ledger.get(grant["grantId"])
        if current and current["status"] == "claimed":
            ledger.update_status(grant["grantId"], "failed")
        raise


if __name__ == "__main__":
    main()
