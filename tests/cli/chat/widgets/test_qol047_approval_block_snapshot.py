"""qol-047 snapshot goldens for the ApprovalBlock Args expansion.

Two goldens:

- ``qol047_approval_collapsed.txt`` — long target, chevron visible.
- ``qol047_approval_expanded.txt`` — same target, Tab pressed once.

Update goldens explicitly:

    SNAPSHOT_UPDATE=1 pytest \\
        tests/cli/chat/widgets/test_qol047_approval_block_snapshot.py

Kept small on purpose — the widget-level snapshots verify the ▸/▾
chevrons + footer-hint suffix land in the compositor exactly the way
the AC #2 mock claims.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget

from cli.chat.widgets.approval_args_row import ApprovalArgsRow
from cli.chat.widgets.approval_block import ApprovalBlock
from tests.cli.chat.snapshot_helpers import assert_snapshot_matches

# Deliberately long: overflows the 70-col Pilot viewport with margin
# on either side, so the compositor definitely truncates.
_LONG_TARGET = (
    "curl -X POST https://example.com/api/v1/endpoint "
    "-d '{\"json\":\"body\",\"more\":\"keys\"}' -H 'auth: Bearer sk-1234'"
)


@pytest.mark.asyncio
async def test_snapshot_collapsed_overflow_shows_chevron(
    mock_app_factory: Callable[[Widget], App],
) -> None:
    """Golden — collapsed row ends with `…▸`, footer hints Tab expand."""
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target=_LONG_TARGET,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(70, 24)) as pilot:
        await pilot.pause()
        # Sanity-check the overflow flag landed before snapshotting.
        row = block.query_one(ApprovalArgsRow)
        assert row.overflowing is True
        assert row.expanded is False
        assert_snapshot_matches(app, "qol047_approval_collapsed")


@pytest.mark.asyncio
async def test_snapshot_expanded_overflow_wraps_target(
    mock_app_factory: Callable[[Widget], App],
) -> None:
    """Golden — expanded row shows `Args  ▾` + wrapped body indented."""
    block = ApprovalBlock(
        rule_id="rl_bash",
        action="kolsch/tools/bash",
        target=_LONG_TARGET,
    )
    app = mock_app_factory(block)
    async with app.run_test(size=(70, 24)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        row = block.query_one(ApprovalArgsRow)
        assert row.expanded is True
        assert_snapshot_matches(app, "qol047_approval_expanded")
