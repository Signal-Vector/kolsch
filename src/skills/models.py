"""Skills value records returned to the engine.

Two Pydantic models, no behaviour beyond validation:

- [`EnabledSkill`][skills.models.EnabledSkill] — one resolved-and-loaded
  skill. The engine consumes the list from
  [`SkillsSubsystem.get_enabled_skills`]
  [skills.subsystem.SkillsSubsystem.get_enabled_skills] to render the
  `{{ skills }}` Jinja variable into the system prompt and to validate
  force-invokes.
- [`SkillInvocation`][skills.models.SkillInvocation] — one prepared
  force-invoke handoff. Carries only the on-disk SKILL.md path that the
  engine passes to a synthetic `read` tool call; authorization is the
  regular `tool.read` check, so the record needs no skill-specific
  action field.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnabledSkill(BaseModel):
    """One enabled skill — the engine-side record.

    Field names are snake_case. The kebab-case frontmatter key
    `disable-model-invocation` is canonicalised by the SKILL.md
    frontmatter parser at catalog build time, so the runtime record
    sees `disable_model_invocation`.

    The record does NOT carry the on-disk SKILL.md path. The listing
    format uses the ref-derived logical path (`<ref>/SKILL.md`); the
    absolute path is only needed for force-invoke and lives on
    [`SkillInvocation`][skills.models.SkillInvocation].
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    disable_model_invocation: bool = False


class SkillInvocation(BaseModel):
    """One prepared force-invoke handoff.

    Two fields, intentionally: `ref` (for engine logging and
    correlation) and the absolute SKILL.md path the engine passes to a
    synthetic `read` tool call. No authz-action field — the engine's
    tool dispatch performs the regular `tool.read` check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    skill_md_path: str
