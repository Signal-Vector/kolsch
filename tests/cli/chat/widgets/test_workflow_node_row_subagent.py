"""bug-036d — `WorkflowNodeRow` as a `ChatMountSink`.

Pre-bug-036d this exercised ``apply_sub_agent_event`` +
``_mount_sub_agent_widget``; both are gone. Post-036d the row is a
``ChatMountSink`` — the adapter's handlers call ``mount_*`` on the
row directly. These regressions cover:

1. Three sink mount calls increment ``_sub_agent_turn_count`` and
   the "N sub-agent events" title.
2. Collapsed row queues mount closures (no live mount) but the
   counter still bumps.
3. Expanded row live-mounts widgets into ``Contents``.
"""

from __future__ import annotations

import pytest
from textual.widgets import Collapsible

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow


@pytest.mark.asyncio
async def test_three_sub_agent_events_increment_count(mock_app_factory) -> None:
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        for i in range(3):
            await row.mount_assistant_message(f"msg-{i}")
        await pilot.pause()
        assert row._sub_agent_turn_count == 3
        assert "3 sub-agent events" in row.title


@pytest.mark.asyncio
async def test_collapsed_row_does_not_mount_sub_agent(
    mock_app_factory,
) -> None:
    """Collapsed row: closures queued in ``_pending_mounts``; counter still bumps."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Row starts collapsed (Collapsible default in __init__).
        assert row.collapsed is True

        await row.mount_assistant_message("hi")
        await pilot.pause()

        # Nothing mounted in Contents yet (queued).
        contents = row.query_one(Collapsible.Contents)
        assert not list(contents.query(AssistantProse))
        # Queued closure present, counter bumped.
        assert len(row._pending_mounts) == 1
        assert row._sub_agent_turn_count == 1


@pytest.mark.asyncio
async def test_expanded_row_mounts_sub_agent(mock_app_factory) -> None:
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        await row.mount_assistant_message("hello")
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        assert list(contents.query(AssistantProse)), (
            "expanded row should live-mount the AssistantProse"
        )
        assert row._sub_agent_turn_count == 1
