"""Skills — skill discovery and progressive disclosure.

Per Module API Reference §10 and docs/explanation/skills-subsystem.md.

Public surface:

- :class:`SkillsSubsystem` — the facade the engine consumes (per-agent
  ref resolution + force-invoke handoff prep).
- :func:`make_standard_validator` — Layer 3 ConfigValidator factory
  the kernel dispatches for ``skills.variant=standard``.
- :class:`EnabledSkill` / :class:`SkillInvocation` — the value shapes
  the engine receives.

qol-skills-jinja-variable-symmetric-with-tools — the
``format_skills_listing`` projection used to emit a system-prompt
block was deleted along with the engine's post-append at
``assembly.py:83-87``. Skills are now rendered into the prompt as
the ``{{ skills }}`` Jinja variable via
:class:`templating.vars.skills.SkillsVar` at pre-flight.

This subsystem emits NO events (Sk-I5). The Tools subsystem's ``read``
dispatch is what materialises skill bodies into the event log.
"""

from skills.models import EnabledSkill, SkillInvocation
from skills.subsystem import SkillsSubsystem
from skills.validator import make_standard_validator

__all__ = [
    "EnabledSkill",
    "SkillInvocation",
    "SkillsSubsystem",
    "make_standard_validator",
]
