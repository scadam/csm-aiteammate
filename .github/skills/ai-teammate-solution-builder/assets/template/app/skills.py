"""Progressive-disclosure skills generated from solution.yaml."""

from __future__ import annotations

from dataclasses import dataclass

from .spec import SolutionSpec


@dataclass(frozen=True)
class RuntimeSkill:
    id: str
    title: str
    description: str
    when_to_use: str
    instructions: str
    capabilities: tuple[str, ...]
    workflows: tuple[str, ...]


class SkillCatalog:
    def __init__(self, spec: SolutionSpec):
        self._direct_capabilities = {
            item.id for item in spec.capabilities if "agent" in item.expose
        }
        self._skills = {
            item.id: RuntimeSkill(
                id=item.id,
                title=item.title,
                description=item.description,
                when_to_use=item.when_to_use,
                instructions=item.instructions,
                capabilities=tuple(item.capabilities),
                workflows=tuple(item.workflows),
            )
            for item in spec.skills
        }

    def list(self) -> list[RuntimeSkill]:
        return list(self._skills.values())

    def get(self, skill_id: str) -> RuntimeSkill:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise KeyError(f"Unknown skill {skill_id!r}") from exc

    def catalog_markdown(self) -> str:
        lines = ["Available skills (load one with get_skill before using its specialist workflow):"]
        for skill in self.list():
            lines.append(
                f"- **{skill.id}**: {skill.description} When to use: {skill.when_to_use}"
            )
        return "\n".join(lines)

    def instructions(self, skill_id: str) -> str:
        skill = self.get(skill_id)
        tools = ", ".join(
            item for item in skill.capabilities if item in self._direct_capabilities
        )
        workflows = ", ".join(f"start_{item}" for item in skill.workflows)
        return (
            f"# {skill.title}\n\n{skill.instructions}\n\n"
            f"Direct tools: {tools}\nPolicy-governed workflow entry points: {workflows}"
        )
