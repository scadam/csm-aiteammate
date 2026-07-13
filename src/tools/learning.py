"""
Skills + memory capabilities (the agent learns and uses packaged know-how).

* ``get_skill`` — load the full instructions of a named **skill** (a Claude-style
  ``SKILL.md``) on demand. The model decides which skill is relevant from the
  catalogue surfaced in its system prompt, then calls this to read it.
* ``remember`` — write a durable learning into the manager's working memory.
* ``recall`` — read the manager's working memory back.

These go through the same ``TOOL_SPECS`` registry as every other capability, so
they are available on the Copilot, reasoning-loop, and MCP surfaces.
"""

from __future__ import annotations

import logging

from .. import identity, memory, skills
from ..observability import execute_tool_scope

logger = logging.getLogger(__name__)


async def get_skill(name: str) -> str:
    """Return the full instructions for a named skill, or the catalogue if unknown."""
    with execute_tool_scope("learning.get_skill", {"name": name}):
        body = skills.load_skill(name)
        if body:
            skill = next((s for s in skills.list_skills() if s.name == name
                          or s.path.lower().endswith(f"/{name.strip().lower()}/skill.md")), None)
            if skill and skill.allowed_tools:
                allowed = ", ".join(skill.allowed_tools)
                return f"{body}\n\n---\nAllowed tools for this skill: {allowed}.\n" \
                       "Prefer these tools while this skill is active; do not use tools outside this list."
            return body
        return "Unknown skill. " + (skills.catalog_markdown() or "No skills available.")


async def remember(note: str, section: str = "Insights") -> str:
    """Record a durable learning in the current manager's working memory."""
    with execute_tool_scope("learning.remember", {"section": section}):
        manager = identity.resolve_manager() or {}
        mid = manager.get("manager_id") or identity.current_manager_id()
        memory.append_learning(mid, section, note, manager.get("display_name", ""))
        return f"Noted in memory under '{section}'."


async def recall() -> str:
    """Return the current manager's working memory."""
    with execute_tool_scope("learning.recall", {}):
        manager = identity.resolve_manager() or {}
        mid = manager.get("manager_id") or identity.current_manager_id()
        return memory.load(mid, manager.get("display_name", ""))
