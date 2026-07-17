#!/usr/bin/env python3
"""Package or install the reusable Copilot agent and its bundled skill."""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SKILL_ROOT.parents[2]
AGENT_SOURCE = SKILL_ROOT.parents[1] / "agents" / "pattern-solution-builder.agent.md"
AGENT_README = SKILL_ROOT.parents[1] / "agents" / "README.md"
PACKAGE_ROOTS = {
    REPOSITORY_ROOT / ".claude-plugin" / "plugin.json": Path(".claude-plugin/plugin.json"),
    REPOSITORY_ROOT / ".claude-plugin" / "marketplace.json": Path(".claude-plugin/marketplace.json"),
    REPOSITORY_ROOT / ".mcp.json": Path(".mcp.json"),
    AGENT_SOURCE: Path(".github/agents/pattern-solution-builder.agent.md"),
    AGENT_README: Path(".github/agents/README.md"),
    SKILL_ROOT: Path(".github/skills/ai-teammate-solution-builder"),
}
FORBIDDEN_PARTS = {".a365", ".venv", "__pycache__", ".pytest_cache", ".state"}
FORBIDDEN_NAMES = {".env", "a365.generated.config.json"}
FORBIDDEN_SUFFIXES = {".key", ".p8", ".p12", ".pem", ".pfx"}


def _included(path: Path) -> bool:
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        return False
    if path.name in FORBIDDEN_NAMES or path.name.startswith("a365.generated.config"):
        return False
    if path.suffix.lower() in FORBIDDEN_SUFFIXES | {".pyc", ".zip"}:
        return False
    return True


def _files() -> list[tuple[Path, Path]]:
    result: list[tuple[Path, Path]] = []
    for source, relative in PACKAGE_ROOTS.items():
        if source.is_file():
            result.append((source, relative))
            continue
        for item in sorted(source.rglob("*")):
            if item.is_file() and _included(item.relative_to(source)):
                result.append((item, relative / item.relative_to(source)))
    return result


def build_archive(output: Path) -> Path:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = _files()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, relative in files:
            archive.write(source, relative.as_posix())
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        required = {
            ".claude-plugin/plugin.json",
            ".claude-plugin/marketplace.json",
            ".mcp.json",
            ".github/agents/pattern-solution-builder.agent.md",
            ".github/agents/README.md",
            ".github/skills/ai-teammate-solution-builder/SKILL.md",
            ".github/skills/ai-teammate-solution-builder/assets/solution.schema.json",
        }
        missing = required - names
        if missing:
            raise RuntimeError(f"Package is incomplete: {sorted(missing)}")
        if any(".." in Path(name).parts or Path(name).is_absolute() for name in names):
            raise RuntimeError("Package contains an unsafe path")
    return output


def install(target: Path, *, force: bool = False) -> Path:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    conflicts = [target / relative for _, relative in _files() if (target / relative).exists()]
    if conflicts and not force:
        preview = ", ".join(str(path.relative_to(target)) for path in conflicts[:5])
        raise FileExistsError(
            f"Target already contains customization files ({preview}). Use --force to replace them."
        )
    for source, relative in _files():
        destination = (target / relative).resolve()
        if not destination.is_relative_to(target):
            raise ValueError(f"Unsafe package destination: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Create a portable ZIP archive")
    mode.add_argument("--install", type=Path, help="Install into a target repository root")
    parser.add_argument("--force", action="store_true", help="Replace existing customization files")
    args = parser.parse_args()
    if args.output:
        print(build_archive(args.output))
    else:
        print(install(args.install, force=args.force))


if __name__ == "__main__":
    main()
