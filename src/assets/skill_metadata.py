"""`SKILL.md` frontmatter parsing.

Skills use the Agent Skills standard: a `SKILL.md` with YAML
frontmatter delimited by `---` lines, followed by the skill body.

This module parses the frontmatter only; the body is loaded lazily
by the engine when a skill is force-invoked or model-read. Catalog
build never loads the body — the catalog scan is read-only with
respect to skill content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from kolsch._yamlio import YAMLError, load

if TYPE_CHECKING:
    from pathlib import Path

_FRONTMATTER_DELIMITER = "---"

FrontmatterErrorKind = Literal["missing", "unterminated", "yaml", "not-mapping"]


@dataclass(frozen=True)
class FrontmatterError:
    """Why a `SKILL.md`'s frontmatter could not be used.

    Attributes:
        kind: ``"missing"`` (the file is empty or its first line is
            not ``---``), ``"unterminated"`` (no closing ``---``),
            ``"yaml"`` (the block is not valid YAML), or
            ``"not-mapping"`` (valid YAML, but not a mapping).
        line: 1-based line in the file the error points at, or
            ``None`` when the parser gave no position.
        column: 1-based column on ``line``, or ``None``.
        message: Human-readable reason. For ``"yaml"`` this is the
            YAML parser's own problem text (e.g. ``mapping values
            are not allowed in this context``).
    """

    kind: FrontmatterErrorKind
    line: int | None
    column: int | None
    message: str


@dataclass(frozen=True)
class SkillFrontmatter:
    """Result of a detailed frontmatter parse.

    Returned by
    [`parse_skill_frontmatter_detailed`][assets.skill_metadata.parse_skill_frontmatter_detailed];
    ``metadata`` is ``{}`` whenever ``error`` is set.
    """

    metadata: dict[str, Any] = field(default_factory=dict)
    error: FrontmatterError | None = None


def parse_skill_frontmatter(skill_md: Path) -> dict[str, Any]:
    """Return the parsed YAML frontmatter as a dict.

    Skills spec keys land verbatim; `disable-model-invocation` is
    canonicalised to `disable_model_invocation` so callers can
    consume it as a Python attribute name without per-key special
    casing. Missing or malformed frontmatter returns `{}` — the
    catalog treats that as "skill present but unusable"; the Skills
    subsystem's Layer 3 validator surfaces the per-skill error at
    agent execute time. Use
    [`parse_skill_frontmatter_detailed`][assets.skill_metadata.parse_skill_frontmatter_detailed]
    to learn *why* the frontmatter was unusable.

    Args:
        skill_md: Filesystem path to the skill's `SKILL.md`.

    Returns:
        The parsed frontmatter mapping, or `{}` when the file is
        absent, the delimiter is missing, or the YAML body fails to
        parse.
    """
    if not skill_md.exists():
        return {}
    return parse_skill_frontmatter_detailed(skill_md).metadata


def parse_skill_frontmatter_detailed(skill_md: Path) -> SkillFrontmatter:
    """Parse a `SKILL.md`'s frontmatter, reporting why it failed.

    Same parse (and `disable-model-invocation` canonicalisation) as
    [`parse_skill_frontmatter`][assets.skill_metadata.parse_skill_frontmatter],
    but a failure carries a [`FrontmatterError`][assets.skill_metadata.FrontmatterError]
    instead of collapsing to `{}`. A YAML error's position is
    translated to file coordinates (the block starts on file line
    2). An empty or comment-only block is not an error: it yields
    `metadata={}` and `error=None`.

    Args:
        skill_md: Filesystem path to an existing `SKILL.md`.

    Returns:
        The parsed metadata, or `{}` plus the error.
    """
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return SkillFrontmatter(
            error=FrontmatterError(
                "missing",
                None,
                None,
                "no YAML frontmatter (SKILL.md must start with a `---` line)",
            )
        )

    end = _find_closing_delimiter(lines)
    if end is None:
        return SkillFrontmatter(
            error=FrontmatterError(
                "unterminated",
                1,
                None,
                "frontmatter opened on line 1 but never closed with `---`",
            )
        )

    frontmatter = "\n".join(lines[1:end])
    try:
        payload = load(frontmatter)
    except YAMLError as exc:
        return SkillFrontmatter(error=_yaml_error(exc))
    if payload is None:
        return SkillFrontmatter()
    if not isinstance(payload, dict):
        return SkillFrontmatter(
            error=FrontmatterError(
                "not-mapping", None, None, "frontmatter must be a YAML mapping"
            )
        )

    # Canonicalise the kebab-case spec key.
    if "disable-model-invocation" in payload:
        payload["disable_model_invocation"] = payload.pop("disable-model-invocation")
    return SkillFrontmatter(metadata=payload)


def _yaml_error(exc: YAMLError) -> FrontmatterError:
    # ruamel's MarkedYAMLError carries ``problem`` + a 0-based
    # ``problem_mark`` relative to the block; duck-typed so ruamel
    # stays imported only by ``kolsch._yamlio``.
    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    return FrontmatterError(
        "yaml",
        line + 2 if isinstance(line, int) else None,
        column + 1 if isinstance(column, int) else None,
        problem if isinstance(problem, str) and problem else str(exc),
    )


def _find_closing_delimiter(lines: list[str]) -> int | None:
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIMITER:
            return i
    return None
