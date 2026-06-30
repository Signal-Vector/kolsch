"""Skill discovery and progressive disclosure.

Implements the Skills subsystem per
[skills subsystem](../../docs/explanation/skills-subsystem.md). The
public surface is a facade the engine consumes ([`SkillsSubsystem`]
[skills.subsystem.SkillsSubsystem]), a Layer 3 validator factory
([`make_standard_validator`][skills.validator.make_standard_validator])
dispatched by the kernel for the `standard` skills variant, and the
two value records the engine receives ([`EnabledSkill`]
[skills.models.EnabledSkill], [`SkillInvocation`]
[skills.models.SkillInvocation]).

This subsystem emits NO events of its own. Skill bodies materialise
into the event log through the Tools subsystem's `read` dispatch —
both for the model's voluntary loads and for the engine's synthetic
force-invoke handoff.
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
