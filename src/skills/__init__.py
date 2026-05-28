"""Skills — skill discovery and progressive disclosure.

Per Module API Reference §10 and docs/explanation/skills-subsystem.md.

Public surface:

- :class:`SkillsSubsystem` — the facade the engine consumes (per-agent
  ref resolution + force-invoke handoff prep).
- :func:`format_skills_listing` — pure projection from
  :class:`EnabledSkill` records to the system-prompt block, honoring
  ``disable-model-invocation: true`` (Sk-I2).
- :func:`make_standard_validator` — Layer 3 ConfigValidator factory
  the kernel dispatches for ``skills.variant=standard``.
- :class:`EnabledSkill` / :class:`SkillInvocation` — the value shapes
  the engine receives.

This subsystem emits NO events (Sk-I5). The Tools subsystem's ``read``
dispatch is what materialises skill bodies into the event log.
"""

from skills.listing import format_skills_listing
from skills.models import EnabledSkill, SkillInvocation
from skills.subsystem import SkillsSubsystem
from skills.validator import make_standard_validator

__all__ = [
    "EnabledSkill",
    "SkillInvocation",
    "SkillsSubsystem",
    "format_skills_listing",
    "make_standard_validator",
]
