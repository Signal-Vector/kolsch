"""Unit tests for the implicit-workspace prefix rule on
``context_files[].path`` — qol-prefix-defaults-and-unified-
refs.md Step 2.

Three behaviors pinned:

- Plain ``<rel>`` paths resolve under the workspace root with
  no deprecation warning.
- Explicit ``workspace:<rel>`` resolves to the SAME target AND
  emits :class:`ConfigDeprecatedRefForm` (the prefix is now
  implicit).
- Explicit ``kolsch:<rel>`` keeps its package-resource lookup
  semantics — no deprecation, since the prefix is still
  required for package resources.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from templating.context_files import resolve_path

if TYPE_CHECKING:
    from pathlib import Path

    from errors import ConfigDeprecatedRefForm


def test_plain_relative_path_resolves_under_workspace_root(tmp_path: Path) -> None:
    target = tmp_path / "bundles" / "X" / "notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("notes", encoding="utf-8")

    warnings: list[ConfigDeprecatedRefForm] = []
    resolved = resolve_path(
        "bundles/X/notes.md", tmp_path, emit_warning=warnings.append
    )
    assert resolved == target.resolve()
    # Canonical form — no deprecation warning fires.
    assert warnings == []


def test_explicit_workspace_prefix_resolves_same_target_with_deprecation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "bundles" / "X" / "notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("notes", encoding="utf-8")

    warnings: list[ConfigDeprecatedRefForm] = []
    resolved = resolve_path(
        "workspace:bundles/X/notes.md",
        tmp_path,
        emit_warning=warnings.append,
    )
    assert resolved == target.resolve()
    assert len(warnings) == 1
    assert warnings[0].detail["deprecated_form"] == "workspace:bundles/X/notes.md"
    assert warnings[0].detail["canonical_form"] == "bundles/X/notes.md"


def test_kolsch_prefix_unchanged(tmp_path: Path) -> None:
    """``kolsch:`` resolves to a package-data path via
    importlib.resources (or the editable-install fallback);
    NO deprecation since the prefix is still required for
    package resources."""
    warnings: list[ConfigDeprecatedRefForm] = []
    resolved = resolve_path(
        "kolsch:docs/index.md", tmp_path, emit_warning=warnings.append
    )
    assert resolved.is_file()
    assert warnings == []
