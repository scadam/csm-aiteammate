"""
Agent skills — Claude/VS Code-style progressive-disclosure skills.

A **skill** is a folder under ``src/skills/<name>/`` containing a ``SKILL.md``
with YAML frontmatter (``name``, ``description``, optional ``when_to_use``) and a
Markdown body of expert instructions. This mirrors the real "Agent Skills"
pattern: only the small **name + description** of each skill is surfaced to the
model up front; the model **decides for itself** (non-deterministically) which
skill is relevant and then loads the **full** instructions on demand via the
``get_skill`` tool. Skills package durable domain know-how separately from the
tools that execute actions.

This loader scans the skill folders once, exposes the catalog for the system
prompt (:func:`catalog_markdown`), and reads a skill's full instructions
(:func:`load_skill`).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_LOCK = threading.RLock()
_cache: dict[str, "Skill"] | None = None


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    when_to_use: str
    body: str
    path: str
    allowed_tools: tuple[str, ...] = ()


def _split_tools(value: str) -> tuple[str, ...]:
    """Parse an inline frontmatter list: ``[a, b]`` or ``a, b`` -> tuple of names."""
    raw = (value or "").strip().strip("[]")
    parts = [p.strip().strip('"').strip("'") for p in raw.split(",")]
    return tuple(p for p in parts if p)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a minimal YAML frontmatter block (key: value) and return (meta, body)."""
    meta: dict = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if m:
        block, body = m.group(1), m.group(2)
        for line in block.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    return meta, body.strip()


def _load_all() -> dict[str, Skill]:
    global _cache
    with _LOCK:
        if _cache is not None:
            return _cache
        skills: dict[str, Skill] = {}
        if _SKILLS_DIR.exists():
            for sub in sorted(_SKILLS_DIR.iterdir()):
                skill_md = sub / "SKILL.md"
                if not (sub.is_dir() and skill_md.exists()):
                    continue
                try:
                    meta, body = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
                except Exception as exc:  # pragma: no cover
                    logger.warning("skill %s failed to load: %s", sub.name, exc)
                    continue
                name = meta.get("name") or sub.name
                allowed = _split_tools(meta.get("allowed-tools") or meta.get("allowed_tools") or "")
                skills[name] = Skill(
                    name=name,
                    description=meta.get("description", ""),
                    when_to_use=meta.get("when_to_use", ""),
                    body=body,
                    path=f"src/skills/{sub.name}/SKILL.md",
                    allowed_tools=allowed,
                )
        _cache = skills
        return skills


def list_skills() -> list[Skill]:
    return list(_load_all().values())


def load_skill(name: str) -> str | None:
    """Return a skill's full instructions (the SKILL.md body), or None if unknown."""
    skills = _load_all()
    skill = skills.get(name)
    if skill is None:  # tolerate folder-name or case-insensitive lookups
        lname = name.strip().lower()
        skill = next((s for s in skills.values()
                      if s.name.lower() == lname or s.path.lower().endswith(f"/{lname}/skill.md")), None)
    return skill.body if skill else None


def catalog_markdown() -> str:
    """A compact catalogue (name + description) to surface in the system prompt."""
    skills = list_skills()
    if not skills:
        return ""
    lines = ["Available skills (load the full instructions with the `get_skill` tool when relevant):"]
    for s in skills:
        hint = f" — {s.when_to_use}" if s.when_to_use else ""
        tools = f" [tools: {', '.join(s.allowed_tools)}]" if s.allowed_tools else ""
        lines.append(f"- **{s.name}**: {s.description}{hint}{tools}")
    return "\n".join(lines)


def reset() -> None:
    global _cache
    with _LOCK:
        _cache = None
