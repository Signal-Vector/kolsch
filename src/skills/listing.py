"""format_skills_listing — Module API Reference §10.1.

Pure projection: takes :class:`EnabledSkill` records and emits the
system-prompt block per Skills spec §5.1. Skills with
``disable_model_invocation: true`` are excluded (Sk-I2); the rest are
emitted as a bulleted list with name, description, and the canonical
SKILL.md path the model uses with the ``read`` tool.

The exact wording is part of the platform's prompt template and may
be tuned; the data conveyed (name + description + read path) is what
matters per spec §5.1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skills.models import EnabledSkill


_LISTING_HEADER = (
    "## Available Skills\n\n"
    "You have access to the following skills. To load a skill's full "
    "instructions, use the `read` tool on its path:\n\n"
)


def format_skills_listing(skills: list[EnabledSkill]) -> str:
    """Format the skills block for the system prompt.

    Returns an empty string when no skills are eligible — either an
    empty input list or every skill marked
    ``disable_model_invocation: true`` (Sk-I2 + spec §5.1).
    """
    eligible = [s for s in skills if not s.disable_model_invocation]
    if not eligible:
        return ""
    lines: list[str] = [_LISTING_HEADER]
    for skill in eligible:
        lines.append(
            f"- **{skill.name}** — {skill.description} Path: `{skill.ref}/SKILL.md`\n",
        )
    return "".join(lines)
