"""spec-014 Test 3 — `ApprovalBlock.parent_hint` (Pilot).

Two passes:

1. `ApprovalBlock(..., parent_hint="workflow foo")` renders a
   leading `Static(" ↳", classes="parent-hint")` before the border
   contents.
2. `ApprovalBlock(..., parent_hint=None)` — no `.parent-hint`
   child is present.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets.approval_block import ApprovalBlock


def _make_block(*, parent_hint: str | None) -> ApprovalBlock:
    return ApprovalBlock(
        rule_id="rl_test",
        action="kolsch/tools/bash",
        target="ls -la",
        parent_hint=parent_hint,
    )


@pytest.mark.asyncio
async def test_parent_hint_renders_arrow_static(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """`parent_hint` set → first `Static` child is ` ↳` with `parent-hint` class."""
    block = _make_block(parent_hint="workflow foo")
    app = fake_app_factory(block)
    async with app.run_test():
        # The first child (`compose` order) must be the parent-hint
        # Static, sitting above the border box's breadcrumb.
        hint_children = [
            c for c in block.children
            if isinstance(c, Static) and c.has_class("parent-hint")
        ]
        assert len(hint_children) == 1
        # Rendered text is `" ↳"` (leading-space + arrow).
        text = str(hint_children[0].render())
        assert "↳" in text


@pytest.mark.asyncio
async def test_no_parent_hint_no_static_arrow(
    fake_app_factory: Callable[[Widget], App],
) -> None:
    """`parent_hint=None` (default) → no `.parent-hint` Static."""
    block = _make_block(parent_hint=None)
    app = fake_app_factory(block)
    async with app.run_test():
        hint_children = [
            c for c in block.children
            if isinstance(c, Static) and c.has_class("parent-hint")
        ]
        assert hint_children == []
