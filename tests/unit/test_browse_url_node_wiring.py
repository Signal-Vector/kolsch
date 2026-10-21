"""Fast wiring tests for the ``browse_url`` reserved built-in node.

Covers Step 4 of ``qol-browse-url-node`` (Tests 3, 3b, 8):

- **Test 3 (reserved slot):** a user-authored file at
  ``bundles/kolsch/nodes/browse_url.py`` raises
  :class:`WorkflowBuiltinShadowed` at scan time.
- **Test 3b (parser wiring):** ``_BUILTIN_NODE_CLASSES["browse_url"]``
  is :class:`BrowseUrlNodeDef` AND ``BrowseUrlNodeDef`` is a member of
  ``typing.get_args(NodeDefUnion)``. This is a regression guard: any
  future dev who defines the class but forgets one of the two
  registrations trips this test.
- **Test 8 (count guard):** ``"browse_url" in BUILTIN_TYPE_NAMES`` AND
  ``len(BUILTIN_TYPE_NAMES) == 16``. Trips loudly if a 17th slot is
  added without updating both this build and its own reservation.
"""

from __future__ import annotations

import typing

import pytest

from assets.catalog import scan
from workflows.catalog_node import BUILTIN_TYPE_NAMES
from workflows.errors import WorkflowBuiltinShadowed
from workflows.parser import (
    _BUILTIN_NODE_CLASSES,
    BrowseUrlNodeDef,
    NodeDefUnion,
)

if typing.TYPE_CHECKING:
    from pathlib import Path


def _make_root(tmp_path: Path) -> Path:
    """Create the canonical platform-root subdirectories under tmp_path."""
    for sub in ("bundles", "sessions", "content", "logs", "data", ".platform"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return tmp_path


def _write_bundle_yaml(bundle_dir: Path, name: str) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "bundle.yaml").write_text(
        f'name: {name}\nversion: "0.1"\ndescription: "test bundle"\n',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Test 3 — Reserved slot.
# ---------------------------------------------------------------------------


def test_browse_url_reserved_slot_raises_shadowed(tmp_path: Path) -> None:
    """A user-authored ``bundles/kolsch/nodes/browse_url.py`` file
    collides with the reserved ``kolsch/nodes/browse_url`` slot and
    must raise :class:`WorkflowBuiltinShadowed` at scan time.
    """
    root = _make_root(tmp_path)
    bundle = root / "bundles" / "kolsch"
    _write_bundle_yaml(bundle, "kolsch")
    nodes_dir = bundle / "nodes"
    nodes_dir.mkdir(exist_ok=True)
    (nodes_dir / "browse_url.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(WorkflowBuiltinShadowed) as exc_info:
        scan(root)
    assert "browse_url" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 3b — Parser wiring assertion.
# ---------------------------------------------------------------------------


def test_browse_url_parser_wiring_registered_in_both_places() -> None:
    """``BrowseUrlNodeDef`` MUST appear in BOTH the built-in class
    table AND the ``NodeDefUnion``. Missing either registration causes
    Pydantic to silently fall through — the dispatcher would route to
    :class:`CatalogNodeDef` instead, skipping the type-specific field
    validation.
    """
    assert _BUILTIN_NODE_CLASSES["browse_url"] is BrowseUrlNodeDef
    assert BrowseUrlNodeDef in typing.get_args(NodeDefUnion)


# ---------------------------------------------------------------------------
# Test 8 — Count guard.
# ---------------------------------------------------------------------------


def test_builtin_type_names_count_is_sixteen_including_browse_url() -> None:
    """``BUILTIN_TYPE_NAMES`` includes ``"browse_url"`` and is exactly
    16 entries (spec-136 added ``jev``). Fires loudly if a 17th slot is
    added without updating both this build and its own reservation.
    """
    assert "browse_url" in BUILTIN_TYPE_NAMES
    assert len(BUILTIN_TYPE_NAMES) == 16
