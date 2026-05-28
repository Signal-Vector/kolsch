"""Skills subsystem errors — spec §4.10."""

from typing import Any

from errors.base import PlatformError
from errors.level import Level


class SkillNotFound(PlatformError):
    """A skill ref does not resolve to a catalogued asset."""

    code = "skill.not_found"
    level = Level.FATAL

    def __init__(
        self,
        ref: str,
        *,
        where: str = "skills.discovery",
    ) -> None:
        super().__init__(
            message=f"Skill {ref!r} not found in catalog",
            where=where,
            detail={"ref": ref},
        )


class SkillMalformed(PlatformError):
    """A SKILL.md file could not be parsed."""

    code = "skill.malformed"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        parse_error: str,
        *,
        where: str = "skills.discovery.parse",
    ) -> None:
        super().__init__(
            message=f"Malformed SKILL.md at {path!r}: {parse_error}",
            where=where,
            detail={"path": path, "parse_error": parse_error},
        )


class SkillFrontmatterInvalid(PlatformError):
    """A SKILL.md's frontmatter failed schema validation."""

    code = "skill.frontmatter_invalid"
    level = Level.FATAL

    def __init__(
        self,
        path: str,
        validation_errors: list[dict[str, Any]],
        *,
        where: str = "skills.discovery.validate",
    ) -> None:
        super().__init__(
            message=(
                f"SKILL.md frontmatter at {path!r} failed validation "
                f"({len(validation_errors)} error(s))"
            ),
            where=where,
            detail={
                "path": path,
                "validation_errors": list(validation_errors),
            },
        )


class SkillForcedNotEnabled(PlatformError):
    """A force-invoke targeted a skill not in the agent's enabled list."""

    code = "skill.forced_not_enabled"
    level = Level.FATAL

    def __init__(
        self,
        ref: str,
        agent_id: str,
        *,
        where: str = "engine.preflight.force_invoke",
    ) -> None:
        super().__init__(
            message=(
                f"Cannot force-invoke skill {ref!r}: not enabled for agent {agent_id!r}"
            ),
            where=where,
            detail={"ref": ref, "agent_id": agent_id},
        )
