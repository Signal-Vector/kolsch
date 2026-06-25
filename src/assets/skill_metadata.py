"""SKILL.md frontmatter parsing — Skills subsystem §3.3 + Asset Mgmt §4.3.

Skills use the Agent Skills standard: a `SKILL.md` with YAML
frontmatter delimited by ``---`` lines, followed by the skill body.

This module parses the frontmatter only; the body is loaded lazily
by the engine when a skill is force-invoked or model-read. Catalog
build never loads the body (Am-I10 + spec §4.3 "body NOT parsed at
catalog build").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from _yamlio import YAMLError, load

if TYPE_CHECKING:
    from pathlib import Path

_FRONTMATTER_DELIMITER = "---"


def parse_skill_frontmatter(skill_md: Path) -> dict[str, Any]:
    """Return the parsed YAML frontmatter as a dict.

    Skills spec keys land verbatim; ``disable-model-invocation`` is
    canonicalised to ``disable_model_invocation`` so callers can
    consume it as a Python attribute name without per-key special
    casing.

    Missing or malformed frontmatter returns ``{}`` (the catalog
    treats that as "skill present but unusable" — Skills' Layer 3
    validator surfaces the per-skill error at agent execute).
    """
    if not skill_md.exists():
        return {}
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIMITER:
        return {}

    end = _find_closing_delimiter(lines)
    if end is None:
        return {}

    frontmatter = "\n".join(lines[1:end])
    try:
        payload = load(frontmatter)
    except YAMLError:
        return {}
    if not isinstance(payload, dict):
        return {}

    # Canonicalise the kebab-case spec key.
    if "disable-model-invocation" in payload:
        payload["disable_model_invocation"] = payload.pop("disable-model-invocation")
    return payload


def _find_closing_delimiter(lines: list[str]) -> int | None:
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIMITER:
            return i
    return None
