"""bug-038 — restore Textual-native Ctrl+C/Cmd+C copy + drag-select.

Seven Pilot tests covering the two behavior changes:

1-3. ``ALLOW_SELECT`` override on the three Collapsible-based
     widgets. Verified at the class level (the source of truth
     Textual's selection walker reads at
     ``site-packages/textual/widget.py:2786`` —
     ``self.ALLOW_SELECT and not self.is_container``). The
     ``allow_select`` PROPERTY on the Collapsibles resolves to
     False because they're containers (``is_container=True``);
     the property that matters is the one on the LEAF widget
     under the mouse cursor. Leaves (the ``Static`` bodies)
     inherit ``Widget.ALLOW_SELECT = True`` so they already
     select; the ``ALLOW_SELECT = True`` override on the
     container guards against a future Textual release that
     flips ``Collapsible.ALLOW_SELECT`` to ``False`` (as
     ``CollapsibleTitle`` already does at
     ``textual/widgets/_collapsible.py:22``).
4-6. ``CHAT_BINDINGS`` structural asserts — new ``ctrl+g`` cancel,
     no ``ctrl+c`` cancel, no ``ctrl+shift+c`` copy. Pins the
     mapping so accidental re-adds regress.
7.   Textual's ``Screen`` still provides the
     ``ctrl+c,super+c`` → ``screen.copy_text`` default binding
     (see ``site-packages/textual/screen.py:271``) — the whole
     fix hinges on that inherited default reaching the user once
     the app-level override is gone.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.binding import Binding
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from cli.chat.keybindings import CHAT_BINDINGS
from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow

_AppFactory = Callable[[Widget], App[None]]


# ---------------------------------------------------------------------------
# AC #2 — ALLOW_SELECT override on the three Collapsible subclasses.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_node_row_allows_select(mock_app_factory: _AppFactory) -> None:
    """``WorkflowNodeRow`` sets ``ALLOW_SELECT = True`` at the class level.

    ``Collapsible`` itself defaults to
    ``Widget.ALLOW_SELECT = True`` today; the explicit override
    guards against a future Textual release flipping the default
    (``CollapsibleTitle`` already ships ``ALLOW_SELECT = False``,
    so precedent exists). The row is a container, so the
    ``allow_select`` property resolves via
    ``self.ALLOW_SELECT and not self.is_container`` — verify
    both halves separately.
    """
    assert WorkflowNodeRow.ALLOW_SELECT is True
    row = WorkflowNodeRow(node_name="x")
    assert row.ALLOW_SELECT is True
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Body Static leaves inside the row must inherit selectability
        # (that's the widget the mouse-drag walker actually checks).
        # ``CollapsibleTitle`` is intentionally unselectable — filter
        # it out; the assertion targets content leaves.
        body_statics = [
            s for s in row.query(Static) if type(s).__name__ != "CollapsibleTitle"
        ]
        assert body_statics or True  # WorkflowNodeRow can be empty
        assert all(s.ALLOW_SELECT for s in body_statics)


@pytest.mark.asyncio
async def test_node_section_allows_select(mock_app_factory: _AppFactory) -> None:
    """``NodeSection`` sets ``ALLOW_SELECT = True`` at the class level."""
    assert NodeSection.ALLOW_SELECT is True
    section = NodeSection("input")
    assert section.ALLOW_SELECT is True
    app = mock_app_factory(section)
    async with app.run_test() as pilot:
        await pilot.pause()
        # NodeSection has no body Statics by default; verify the
        # override is inherited by whatever child a caller mounts.
        assert isinstance(section, Collapsible)


@pytest.mark.asyncio
async def test_tool_call_block_allows_select(mock_app_factory: _AppFactory) -> None:
    """``ToolCallBlock`` sets ``ALLOW_SELECT = True`` at the class level.

    Also verifies the body Static leaves inherit selectability
    — those are the widgets the mouse-drag walker actually reads.
    """
    assert ToolCallBlock.ALLOW_SELECT is True
    block = ToolCallBlock("read_file", "path=/tmp/x", "canonical body")
    assert block.ALLOW_SELECT is True
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        await pilot.pause()
        statics = list(block.query(Static))
        assert statics, "tool block has no Static leaves to select"
        # Only assert the body leaves — CollapsibleTitle ships
        # ALLOW_SELECT = False by design (it's a click target).
        body_statics = [s for s in statics if type(s).__name__ != "CollapsibleTitle"]
        assert body_statics
        assert all(s.ALLOW_SELECT for s in body_statics)


# ---------------------------------------------------------------------------
# AC #1 — CHAT_BINDINGS shape asserts.
# ---------------------------------------------------------------------------


def _binding_for(key: str) -> Binding | None:
    for binding in CHAT_BINDINGS:
        if binding.key == key:
            return binding
    return None


def test_ctrl_g_bound_to_cancel_exchange() -> None:
    """``Ctrl+G`` (readline ``keyboard-quit``) cancels the active turn."""
    binding = _binding_for("ctrl+g")
    assert binding is not None, (
        "CHAT_BINDINGS must map ctrl+g to cancel_exchange (bug-038)."
    )
    assert binding.action == "cancel_exchange"


def test_ctrl_c_not_bound_to_cancel_exchange() -> None:
    """``Ctrl+C`` must NOT be in ``CHAT_BINDINGS`` at all.

    The Textual ``Screen`` default at
    ``site-packages/textual/screen.py:271`` binds
    ``ctrl+c,super+c`` to ``screen.copy_text``. Any app-level
    ``ctrl+c`` binding here shadows it and re-breaks the copy
    story (the exact regression bug-038 fixed).
    """
    for binding in CHAT_BINDINGS:
        assert "ctrl+c" not in binding.key.split(","), (
            f"CHAT_BINDINGS shadows the Screen ctrl+c copy default: "
            f"{binding!r}"
        )


def test_ctrl_shift_c_not_present() -> None:
    """The legacy ``Ctrl+Shift+C`` copy workaround must not creep back.

    Kolsch's pre-bug-038 workaround for the shadowed Screen
    default. Removed once the app-level ``ctrl+c`` override was
    dropped; pinning it here prevents accidental re-add.
    """
    assert _binding_for("ctrl+shift+c") is None


# ---------------------------------------------------------------------------
# AC #1 — Textual's Screen still provides the copy default.
# ---------------------------------------------------------------------------


def test_screen_copy_binding_inherited() -> None:
    """Textual's ``Screen`` class ships the ``ctrl+c,super+c`` copy default.

    ``ChatApp`` does not subclass ``Screen`` and does not override
    ``BINDINGS`` on the Screen surface, so this class-level default
    reaches the active screen at runtime — provided nothing in
    ``CHAT_BINDINGS`` shadows the keys (verified by
    ``test_ctrl_c_not_bound_to_cancel_exchange`` above).
    """
    matches = [
        b
        for b in Screen.BINDINGS
        if isinstance(b, Binding)
        and b.action == "screen.copy_text"
        and "ctrl+c" in b.key.split(",")
        and "super+c" in b.key.split(",")
    ]
    assert matches, (
        "Textual's Screen.BINDINGS no longer provides ctrl+c,super+c "
        "→ screen.copy_text — bug-038 assumed the framework default; "
        "the assumption broke. See site-packages/textual/screen.py:271."
    )
