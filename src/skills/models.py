"""Skills value types — Module API Reference §10.2.

The two records the skills subsystem returns to the engine:

- :class:`EnabledSkill` — one resolved-and-loaded skill record. The
  engine consumes the list returned by
  :meth:`SkillsSubsystem.get_enabled_skills` to build the system-prompt
  block (via :func:`skills.listing.format_skills_listing`) and to
  validate force-invokes.
- :class:`SkillInvocation` — the force-invoke handoff. Carries only
  the SKILL.md path the engine will pass to a synthetic ``read`` tool
  call (Sk-I4 + spec §6.2/§6.3). There is intentionally no
  skill-specific authz action — authorization is the regular
  ``tool.read`` check.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnabledSkill(BaseModel):
    """One enabled skill — Module API Reference §10.2.

    Field names are snake_case to match the reference. The kebab-case
    spec key ``disable-model-invocation`` is canonicalised here (the
    SKILL.md frontmatter parser handles the rename at catalog build).

    Note: this record does NOT carry the on-disk SKILL.md path. The
    listing format uses the ref-derived logical path
    (``<ref>/SKILL.md``); the on-disk absolute path is only needed for
    force-invoke and lives on :class:`SkillInvocation`.
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
    """One prepared force-invoke handoff — Module API Reference §10.2.

    Two fields, intentionally: the ref (for engine logging /
    correlation) and the absolute SKILL.md path the engine will pass
    to a synthetic ``read`` tool call. No authz-action field — the
    engine's tool dispatch performs the regular ``tool.read`` check
    (Sk-I4 + spec §6.3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    skill_md_path: str
