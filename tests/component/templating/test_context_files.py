"""``context_files`` resolution + loading — Phase 2.2 Step 12.

Pins (post qol-prefix-defaults-and-unified-refs.md Step 2):

- ``workspace:<rel>`` resolves under the workspace root AND
  emits :class:`ConfigDeprecatedRefForm` (the prefix is now
  implicit; explicit ``workspace:`` is deprecated).
- ``kolsch:<rel>`` resolves to a real file under the installed
  package (works in editable-dev too via the source-tree
  fallback). NO deprecation — ``kolsch:`` stays required for
  package resources.
- Plain ``<rel>`` paths resolve under the workspace root with no
  warning — this is the canonical form.
- Missing file → :class:`ContextFileNotFound`.
- 64 KiB cap enforced via :meth:`Path.stat` before reading.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from errors import (
    ConfigDeprecatedRefForm,
    ContextFileNotFound,
    ContextFileTooLarge,
)
from schemas import ContextFileSpec
from templating.context_files import (
    DEFAULT_MAX_BYTES,
    load_context_file,
    resolve_path,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# resolve_path.
# ---------------------------------------------------------------------------


def test_workspace_prefix_resolves_under_workspace_root(tmp_path: Path) -> None:
    """Explicit ``workspace:`` still resolves but is now deprecated
    — sink captures the ConfigDeprecatedRefForm emit."""
    (tmp_path / "CONVENTIONS.md").write_text("conventions", encoding="utf-8")
    warnings: list[ConfigDeprecatedRefForm] = []
    path = resolve_path(
        "workspace:CONVENTIONS.md",
        tmp_path,
        emit_warning=warnings.append,
    )
    assert path == (tmp_path / "CONVENTIONS.md").resolve()
    assert len(warnings) == 1
    assert warnings[0].detail["deprecated_form"] == "workspace:CONVENTIONS.md"
    assert warnings[0].detail["canonical_form"] == "CONVENTIONS.md"


def test_workspace_prefix_resolves_nested_path(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("x", encoding="utf-8")
    warnings: list[ConfigDeprecatedRefForm] = []
    path = resolve_path("workspace:docs/x.md", tmp_path, emit_warning=warnings.append)
    assert path == (tmp_path / "docs" / "x.md").resolve()
    assert len(warnings) == 1


def test_kolsch_prefix_resolves_real_repo_doc(tmp_path: Path) -> None:
    """The wheel ships ``docs/`` as package data under ``kolsch``
    and the editable-install fallback walks the source tree.
    Either way, ``kolsch:docs/index.md`` resolves to a real file."""
    warnings: list[ConfigDeprecatedRefForm] = []
    path = resolve_path("kolsch:docs/index.md", tmp_path, emit_warning=warnings.append)
    assert path.is_file()
    # ``kolsch:`` stays required for package resources — no deprecation.
    assert warnings == []


def test_plain_path_resolves_under_workspace_root(tmp_path: Path) -> None:
    """Plain paths are the canonical form per
    qol-prefix-defaults-and-unified-refs.md — no prefix needed."""
    (tmp_path / "plain.md").write_text("plain", encoding="utf-8")
    warnings: list[ConfigDeprecatedRefForm] = []
    path = resolve_path("plain.md", tmp_path, emit_warning=warnings.append)
    assert path == (tmp_path / "plain.md").resolve()
    assert warnings == []


def test_nested_plain_path_resolves_under_workspace_root(tmp_path: Path) -> None:
    """``bundles/X/notes.md`` no longer needs an explicit
    ``workspace:`` prefix."""
    (tmp_path / "bundles" / "X").mkdir(parents=True)
    (tmp_path / "bundles" / "X" / "notes.md").write_text("n", encoding="utf-8")
    warnings: list[ConfigDeprecatedRefForm] = []
    path = resolve_path("bundles/X/notes.md", tmp_path, emit_warning=warnings.append)
    assert path == (tmp_path / "bundles" / "X" / "notes.md").resolve()
    assert warnings == []


# ---------------------------------------------------------------------------
# load_context_file.
# ---------------------------------------------------------------------------


def test_load_context_file_populates_doc_item(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("# Heading\n\nbody", encoding="utf-8")
    spec = ContextFileSpec(
        path="workspace:x.md", summary="Author's summary.", primary=True
    )
    doc = load_context_file(spec, tmp_path)
    assert doc.path == "workspace:x.md"
    assert doc.summary == "Author's summary."
    assert doc.primary is True
    assert doc.content == "# Heading\n\nbody"
    assert doc.size_bytes == len("# Heading\n\nbody")


def test_load_context_file_summary_fallback_first_non_empty_line(
    tmp_path: Path,
) -> None:
    """When the spec has no summary, derive from the file's first
    non-empty line — Phase 2.2 §6.1."""
    (tmp_path / "x.md").write_text("\n\n   first real line\nbelow\n", encoding="utf-8")
    spec = ContextFileSpec(path="workspace:x.md")
    doc = load_context_file(spec, tmp_path)
    assert doc.summary == "first real line"


def test_load_missing_file_raises_not_found(tmp_path: Path) -> None:
    spec = ContextFileSpec(path="workspace:ghost.md")
    with pytest.raises(ContextFileNotFound) as excinfo:
        load_context_file(spec, tmp_path)
    assert excinfo.value.detail["path"] == "workspace:ghost.md"


def test_size_cap_enforced_at_64_kib_plus_one(tmp_path: Path) -> None:
    """64 KiB + 1 byte → reject. Boundary tested per Phase 2.2 §6.4."""
    payload = "x" * (DEFAULT_MAX_BYTES + 1)
    (tmp_path / "big.md").write_text(payload, encoding="utf-8")
    spec = ContextFileSpec(path="workspace:big.md")
    with pytest.raises(ContextFileTooLarge) as excinfo:
        load_context_file(spec, tmp_path)
    err = excinfo.value
    assert err.detail["size_bytes"] == DEFAULT_MAX_BYTES + 1
    assert err.detail["max_bytes"] == DEFAULT_MAX_BYTES


def test_size_cap_accepts_exactly_64_kib(tmp_path: Path) -> None:
    """64 KiB exactly → loads cleanly."""
    payload = "x" * DEFAULT_MAX_BYTES
    (tmp_path / "atcap.md").write_text(payload, encoding="utf-8")
    spec = ContextFileSpec(path="workspace:atcap.md")
    doc = load_context_file(spec, tmp_path)
    assert doc.size_bytes == DEFAULT_MAX_BYTES
