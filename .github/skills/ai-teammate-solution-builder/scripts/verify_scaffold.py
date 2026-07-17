#!/usr/bin/env python3
"""Generate the bundled example and run its tests with the current interpreter."""

from __future__ import annotations

import argparse
import copy
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

from package_agent import build_archive, install
from scaffold import ASSETS, _validate, scaffold
from studio.core import DraftStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Keep the generated directory")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Install each generated project in a fresh venv and run pip check/tests",
    )
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="ai-teammate-scaffold-"))
    output = root / "incident-teammate"
    try:
        _verify_studio()
        scaffold(ASSETS / "solution.example.yaml", output)
        subprocess.run([sys.executable, "-m", "compileall", "-q", "app", "tests"], cwd=output, check=True)
        _verify_project(output, clean=args.clean)
        mcp_only_spec = _mcp_only_spec(root)
        mcp_only_output = root / "collaboration-teammate"
        scaffold(mcp_only_spec, mcp_only_output)
        assert (mcp_only_output / "openapi").is_dir()
        assert (mcp_only_output / "data").is_dir()
        _assert_path_traversal_rejected()
        _verify_project(mcp_only_output, clean=args.clean)
        salesforce_spec = ASSETS / "examples" / "salesforce-outreach" / "solution.yaml"
        salesforce_output = root / "salesforce-outreach-teammate"
        scaffold(salesforce_spec, salesforce_output)
        _verify_project(salesforce_output, clean=args.clean)
        _verify_portable_package(root)
        print(f"Verified generated solution at {output}")
    finally:
        if args.keep:
            print(f"Kept {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


def _verify_studio() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            str(ASSETS.parent / "tests" / "test_studio.py"),
        ],
        cwd=ASSETS.parents[3],
        check=True,
    )


def _verify_project(output: Path, *, clean: bool) -> None:
    subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app", "tests"],
        cwd=output,
        check=True,
    )
    python = sys.executable
    if clean:
        venv = output / ".verify-venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = str(venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))
        subprocess.run([python, "-m", "pip", "install", "--quiet", "-e", ".[dev]"], cwd=output, check=True)
        subprocess.run([python, "-m", "pip", "check"], cwd=output, check=True)
    subprocess.run([python, "-m", "pytest", "-q"], cwd=output, check=True)
    az = shutil.which("az")
    if az:
        compiled = output / ".verify-main.json"
        subprocess.run(
            [az, "bicep", "build", "--file", str(output / "infra/main.bicep"), "--outfile", str(compiled)],
            cwd=output,
            check=True,
        )
        compiled.unlink(missing_ok=True)


def _mcp_only_spec(root: Path) -> Path:
    payload = yaml.safe_load((ASSETS / "solution.example.yaml").read_text(encoding="utf-8"))
    payload["solution"].update(
        {
            "id": "collaboration_teammate",
            "name": "Collaboration Teammate",
            "description": "Coordinates collaboration context using an existing MCP server.",
        }
    )
    payload["agent"]["display_name"] = "Collaboration Teammate"
    payload["openapi_sources"] = []
    payload["capabilities"] = [
        item
        for item in payload["capabilities"]
        if item["kind"] != "openapi_operation"
    ]
    capability_ids = {item["id"] for item in payload["capabilities"]}
    for skill in payload["skills"]:
        skill["capabilities"] = [
            item for item in skill["capabilities"] if item in capability_ids
        ]
    for workflow in payload["workflows"]:
        workflow["stages"] = [
            stage
            for stage in workflow["stages"]
            if not stage.get("capability") or stage["capability"] in capability_ids
        ]
    target = root / "collaboration.solution.yaml"
    target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return target


def _assert_path_traversal_rejected() -> None:
    source = ASSETS / "solution.example.yaml"
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    payload["openapi_sources"][0]["document"] = "../outside.yaml"
    try:
        _validate(payload, source)
    except ValueError as exc:
        assert "contained" in str(exc) or "escapes" in str(exc)
    else:
        raise AssertionError("OpenAPI traversal path was accepted")


def _verify_portable_package(root: Path) -> None:
    archive_path = build_archive(root / "ai-teammate-solution-builder.zip")
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert ".claude-plugin/plugin.json" in names
        assert ".claude-plugin/marketplace.json" in names
        assert ".mcp.json" in names
        assert ".github/agents/pattern-solution-builder.agent.md" in names
        assert ".github/agents/README.md" in names
        assert ".github/skills/ai-teammate-solution-builder/SKILL.md" in names
        assert ".github/skills/ai-teammate-solution-builder/scripts/package_agent.py" in names
        assert ".github/skills/ai-teammate-solution-builder/assets/examples/salesforce-outreach/solution.yaml" in names
        forbidden_suffixes = {".env", ".key", ".p8", ".p12", ".pem", ".pfx", ".pyc"}
        assert not any(Path(name).suffix.lower() in forbidden_suffixes for name in names)
        extracted = root / "archive-install"
        archive.extractall(extracted)

    direct = root / "direct-install"
    install(direct)
    for target in (extracted, direct):
        agent = target / ".github/agents/pattern-solution-builder.agent.md"
        skill = target / ".github/skills/ai-teammate-solution-builder/SKILL.md"
        assert _frontmatter(agent)["name"] == "AI Teammate Solution Builder"
        assert _frontmatter(skill)["name"] == "ai-teammate-solution-builder"

    installed_skill = direct / ".github/skills/ai-teammate-solution-builder"
    installed_output = root / "installed-salesforce-outreach"
    installed_store = DraftStore(
        root / "installed-studio",
        installed_skill / "assets/solution.schema.json",
    )
    installed_store.ingest(
        "# Installed plugin smoke test\n",
        {"sourceName": "smoke.md", "sourceType": "markdown", "warnings": []},
    )
    installed_store.seed_from(
        installed_skill / "assets/examples/salesforce-outreach/solution.yaml"
    )
    installed_store.confirm(action="scaffold", output_path=installed_output)
    subprocess.run(
        [
            sys.executable,
            str(installed_skill / "scripts/scaffold.py"),
            "--spec",
            str(installed_store.draft_file),
            "--output",
            str(installed_output),
            "--confirmation-file",
            str(installed_store.confirmation_file),
        ],
        check=True,
    )
    assert (installed_output / "app/agent.py").is_file()
    assert (installed_output / "infra/main.bicep").is_file()


def _frontmatter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"Missing YAML frontmatter: {path}")
    _, raw, _ = text.split("---", 2)
    value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise AssertionError(f"Invalid YAML frontmatter: {path}")
    return value


if __name__ == "__main__":
    main()
