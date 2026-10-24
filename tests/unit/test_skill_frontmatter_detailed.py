"""bug-149 — ``parse_skill_frontmatter_detailed`` error kinds + positions.

Also pins that ``parse_skill_frontmatter`` (the catalog's parser)
keeps its ``{}``-on-failure contract while delegating to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from assets.skill_metadata import (
    FrontmatterError,
    parse_skill_frontmatter,
    parse_skill_frontmatter_detailed,
)

if TYPE_CHECKING:
    from pathlib import Path


def _skill(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("", "missing"),
        ("# Skill: x\n\nNo frontmatter here.\n", "missing"),
        ("---\nname: x\n", "unterminated"),
        ('---\nname: x\ndescription: a b: "c"\n---\nbody\n', "yaml"),
        ("---\n- a\n---\nbody\n", "not-mapping"),
    ],
)
def test_frontmatter_detailed_error_kinds(tmp_path: Path, text: str, kind: str) -> None:
    parsed = parse_skill_frontmatter_detailed(_skill(tmp_path, text))
    assert parsed.error is not None
    assert parsed.error.kind == kind
    assert parsed.metadata == {}
    # The catalog's parser still collapses every failure to ``{}``.
    assert parse_skill_frontmatter(_skill(tmp_path, text)) == {}


def test_frontmatter_detailed_yaml_error_is_in_file_coordinates(
    tmp_path: Path,
) -> None:
    """The block starts on file line 2; the column points at the
    offending ``:`` (1-based)."""
    text = '---\nname: x\ndescription: a b: "c"\n---\nbody\n'
    error = parse_skill_frontmatter_detailed(_skill(tmp_path, text)).error
    assert error is not None
    assert error.line == 3
    assert error.column == len("description: a b:")
    assert "mapping values are not allowed" in error.message


def test_frontmatter_detailed_unterminated_and_missing_positions(
    tmp_path: Path,
) -> None:
    unterminated = parse_skill_frontmatter_detailed(_skill(tmp_path, "---\nname: x\n"))
    assert unterminated.error == FrontmatterError(
        "unterminated",
        1,
        None,
        "frontmatter opened on line 1 but never closed with `---`",
    )
    missing = parse_skill_frontmatter_detailed(_skill(tmp_path, "# x\n"))
    assert missing.error is not None
    assert (missing.error.line, missing.error.column) == (None, None)


@pytest.mark.parametrize("block", ["", "# only a comment\n"])
def test_frontmatter_detailed_empty_block_is_not_an_error(
    tmp_path: Path, block: str
) -> None:
    parsed = parse_skill_frontmatter_detailed(
        _skill(tmp_path, f"---\n{block}---\nbody\n")
    )
    assert parsed.error is None
    assert parsed.metadata == {}


def test_frontmatter_detailed_valid_matches_wrapper(tmp_path: Path) -> None:
    """Valid input: both parsers return the same dict, including the
    ``disable-model-invocation`` canonicalisation."""
    path = _skill(
        tmp_path,
        "---\nname: x\ndescription: Does x.\ndisable-model-invocation: true\n---\nbody\n",
    )
    expected = {"name": "x", "description": "Does x.", "disable_model_invocation": True}
    parsed = parse_skill_frontmatter_detailed(path)
    assert parsed.error is None
    assert parsed.metadata == expected
    assert parse_skill_frontmatter(path) == expected


def test_frontmatter_wrapper_missing_file_returns_empty(tmp_path: Path) -> None:
    assert parse_skill_frontmatter(tmp_path / "absent" / "SKILL.md") == {}


def test_frontmatter_detailed_yaml_message_is_one_bounded_line(
    tmp_path: Path,
) -> None:
    """A duplicate-key error echoes both values verbatim; the message is
    whitespace-collapsed and capped so it fits a one-line surface."""
    long_value = "".join(f"  line {i} of a long description\n" for i in range(80))
    text = (
        f"---\nname: x\ndescription: |\n{long_value}"
        f"description: |\n{long_value}---\nbody\n"
    )
    error = parse_skill_frontmatter_detailed(_skill(tmp_path, text)).error
    assert error is not None
    assert error.kind == "yaml"
    assert "duplicate key" in error.message
    assert "\n" not in error.message
    assert len(error.message) <= 200
    assert error.message.endswith("…")


def test_frontmatter_bom_is_ignored_by_both_parsers(tmp_path: Path) -> None:
    """A UTF-8 BOM before the opening ``---`` parses normally (the
    catalog's wrapper used to return ``{}`` for such files)."""
    path = tmp_path / "SKILL.md"
    path.write_bytes(b"\xef\xbb\xbf---\nname: x\ndescription: Does x.\n---\nbody\n")
    expected = {"name": "x", "description": "Does x."}
    parsed = parse_skill_frontmatter_detailed(path)
    assert parsed.error is None
    assert parsed.metadata == expected
    assert parse_skill_frontmatter(path) == expected
