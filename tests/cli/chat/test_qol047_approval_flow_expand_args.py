"""Integration tests for qol-047 — Args expansion through ``ApprovalBlock``.

Both AC #8 tests:

1. ``test_approval_flow_expand_args_visible_before_choice`` — user
   opens the approval prompt with a long target, presses Tab, sees the
   full command, then presses ``y`` to allow. The resolution must fire
   with ``AuthzEffect.ALLOW`` unchanged — Tab must not have interfered
   with the choice flow.

2. ``test_approval_flow_tab_noop_when_target_short`` — with a short
   target, pressing Tab is a no-op. The full rendered screen snapshot
   is identical before and after.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets.approval_args_row import ApprovalArgsRow
from cli.chat.widgets.approval_block import ApprovalBlock
from providers.authz import AuthzEffect


def _rendered_row_text(row: ApprovalArgsRow) -> str:
    return str(row.query_one(".args-line", Static).render())


@pytest.mark.asyncio
async def test_approval_flow_expand_args_visible_before_choice(
    mock_app_factory: Callable[[Widget], App],
) -> None:
    """Full flow: mount long target → chevron → Tab → visible → ``y`` → ALLOW."""
    long_target = (
        "curl -X POST https://example.com/very/long/api/endpoint?a=1&b=2 "
        "-d '{\"json\":\"payload\"}' -H 'authorization: Bearer sk-abc-xyz'"
    )
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target=long_target,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(70, 24)) as pilot:
        # (1) The args row detects overflow at first mount → shows ``▸``.
        await pilot.pause()
        row = block.query_one(ApprovalArgsRow)
        assert row.overflowing is True
        text = _rendered_row_text(row)
        assert "▸" in text, (
            "Collapsed overflow state must show ▸ chevron in Args row"
        )
        # (2) Press Tab — Args row expands, full target becomes visible.
        await pilot.press("tab")
        await pilot.pause()
        assert row.expanded is True
        expanded_text = _rendered_row_text(row)
        assert long_target in expanded_text.replace("\n", "").replace(" ", "") or (
            # Char-wrap: the joined body-without-indent matches
            long_target
            in "".join(
                line.lstrip()
                for line in expanded_text.splitlines()[1:]
            )
        )
        # (3) Press ``y`` — the choice pane's binding still fires; the
        # widget resolves with ALLOW. Tab must NOT have hijacked ``y``.
        await pilot.press("y")
        result = await asyncio.wait_for(block.resolved(), timeout=2.0)
        assert result is AuthzEffect.ALLOW


@pytest.mark.asyncio
async def test_approval_flow_tab_noop_when_target_short(
    mock_app_factory: Callable[[Widget], App],
) -> None:
    """A short target: pressing Tab does not change the row's rendering.

    The row's ``expanded`` flag stays False (no overflow → no toggle),
    and the rendered text is byte-identical before and after Tab.
    """
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target="ls -la",
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        row = block.query_one(ApprovalArgsRow)
        assert row.overflowing is False
        before = _rendered_row_text(row)
        await pilot.press("tab")
        await pilot.pause()
        assert row.expanded is False, (
            "Tab on non-overflowing row must be a visual no-op"
        )
        after = _rendered_row_text(row)
        assert before == after, (
            f"Rendered row changed on Tab despite no overflow:\n"
            f"  before: {before!r}\n"
            f"  after:  {after!r}"
        )
