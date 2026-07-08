"""spec-013 Test 5 — Error preview truncation on `WorkflowNodeRow`.

Two passes:

1. A long error message (200 chars) — truncated to ~60 chars with a
   `...` suffix in the row's title.
2. A short error message (30 chars) — renders untruncated.
"""

from __future__ import annotations

import pytest

from cli.chat.widgets.workflow_node_row import WorkflowNodeRow


@pytest.mark.asyncio
async def test_long_error_message_truncated_in_title(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        long_msg = "E" * 200
        row.set_state("failed", error_preview=long_msg)
        await pilot.pause()
        assert row.state == "failed"
        # Truncated: title does not contain the full 200-char string.
        assert long_msg not in row.title
        # `...` suffix marks the truncation.
        assert "..." in row.title
        # Fixed 60-char lock (Coordinator #4). The row's truncate
        # helper caps preview length at 60 chars.
        truncated = row._truncate_preview(long_msg)
        assert len(truncated) == 60
        assert truncated.endswith("...")


@pytest.mark.asyncio
async def test_short_error_message_untruncated(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        short_msg = "boom: filesystem read denied"  # 28 chars
        assert len(short_msg) < 60
        row.set_state("failed", error_preview=short_msg)
        await pilot.pause()
        assert row.state == "failed"
        assert short_msg in row.title
        # No truncation marker.
        preview = row._truncate_preview(short_msg)
        assert preview == short_msg
