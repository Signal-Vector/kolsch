"""spec-021 (bug-036d shape) — pending-mount queue + backfill on expand.

bug-036d reshapes spec-021: the row buffers *mount closures*, not
raw events. Users who expand a row AFTER a sub-agent has produced
events see those mounts replayed into ``Contents`` in the order the
sink methods were called. Terminal-state rows freeze the queue.

Sub-tests:

1. Sink calls while collapsed queue closures + bump the counter;
   nothing lands in ``Contents``.
2. Expanding drains the queue in insertion order — the widgets
   land inside ``Contents``.
3. Live-mounted events (row expanded when call arrived) don't get
   re-mounted on a collapse -> expand cycle (dedupe by event_id).
4. Overflow past 50 pending mounts drops the oldest closure.
5. Terminal ``set_state`` freezes further mounts.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Collapsible

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.workflow_node_row import (
    _EVENT_BUFFER_MAX,
    WorkflowNodeRow,
)

_AppFactory = Callable[[Widget], App[None]]


@pytest.mark.asyncio
async def test_events_while_collapsed_queue_closures(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #2 + AC #3 — collapsed row buffers without live-mounting."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert row.collapsed is True

        for i in range(3):
            await row.mount_assistant_message(f"m{i}", event_id=f"ev-{i}")
        await pilot.pause()

        # Queued (not live-mounted).
        assert len(row._pending_mounts) == 3
        assert row._sub_agent_turn_count == 3
        contents = row.query_one(Collapsible.Contents)
        assert not list(contents.query(AssistantProse))
        assert "3 sub-agent events" in row.title


@pytest.mark.asyncio
async def test_expand_drains_queue_in_order(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #4 — collapsed -> expanded drains the queue in insertion order."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("first", event_id="ev-a")
        await row.mount_assistant_message("second", event_id="ev-b")
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        assert not list(contents.query(AssistantProse))

        row.collapsed = False
        # Drain runs via `call_after_refresh`; two pauses guarantee it.
        await pilot.pause()
        await pilot.pause()

        prose = list(contents.query(AssistantProse))
        assert len(prose) == 2, f"expected two AssistantProse widgets; got {len(prose)}"


@pytest.mark.asyncio
async def test_live_mounted_events_do_not_replay_on_recollapse(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #5 — dedupe by event_id across live-mount + replay.

    Row starts expanded; events live-mount. Collapse and re-expand;
    the drain finds the dedupe keys stamped and skips.
    """
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.collapsed = False
        await pilot.pause()

        for i in range(3):
            await row.mount_assistant_message(f"m{i}", event_id=f"ev-{i}")
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        assert len(list(contents.query(AssistantProse))) == 3

        # Collapse and re-expand — no re-mount.
        row.collapsed = True
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        assert len(list(contents.query(AssistantProse))) == 3


@pytest.mark.asyncio
async def test_overflow_drops_oldest(mock_app_factory: _AppFactory) -> None:
    """AC #6 — 51st queued closure drops the oldest."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        for i in range(_EVENT_BUFFER_MAX + 1):
            await row.mount_assistant_message(f"m{i}", event_id=f"ev-{i}")
        await pilot.pause()

        assert len(row._pending_mounts) == _EVENT_BUFFER_MAX
        # Turn count is NOT capped — reflects total arrivals.
        assert row._sub_agent_turn_count == _EVENT_BUFFER_MAX + 1


@pytest.mark.asyncio
async def test_terminal_state_freezes_queue(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #7 — terminal transition freezes further mounts."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("pre-a", event_id="ev-a")
        await row.mount_assistant_message("pre-b", event_id="ev-b")
        await pilot.pause()

        assert row._sub_agent_turn_count == 2
        assert len(row._pending_mounts) == 2

        row.set_state("completed", details="done")
        await pilot.pause()

        # Post-terminal mounts are dropped: neither the queue nor
        # the counter grow.
        await row.mount_assistant_message("post-a", event_id="ev-c")
        await row.mount_assistant_message("post-b", event_id="ev-d")
        await pilot.pause()

        assert row._sub_agent_turn_count == 2
