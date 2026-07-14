"""bug-036b — row-title label regressions.

Pilot-driven tests covering the two label changes:

1. Type suffix (``▸ node_name (agent)``) shown pre-completion when
   ``_details`` is empty; DROPPED once ``_details`` populates.
2. Counter label reads ``sub-agent events`` (not ``turns``) — the
   count includes non-widget-mounting events, so ``events`` is
   the honest label.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId


def _mk_agent_event() -> Event:
    """Build a well-formed `AGENT_MESSAGE` `Event`.

    Each call generates a fresh ``event_id`` so the dedupe set in
    the row's ``_mounted_event_ids`` gets a distinct key per event.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.AGENT_MESSAGE,
        data={"content": {"type": "text", "text": "hi"}},
        actor=Actor(kind="system"),
        created_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_row_title_shows_type_pre_completion(mock_app_factory: Any) -> None:
    """`(agent)` suffix is present in a running row with no details."""
    row = WorkflowNodeRow(
        node_name="hiring_manager_review",
        node_type="kolsch/nodes/agent",
    )
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("running")
        await pilot.pause()
        assert "(agent)" in row.title
        assert "hiring_manager_review" in row.title


@pytest.mark.asyncio
async def test_row_title_drops_type_when_details_set(
    mock_app_factory: Any,
) -> None:
    """Post-completion — ``_details`` populated — the suffix is dropped."""
    row = WorkflowNodeRow(
        node_name="hiring_manager_review",
        node_type="kolsch/nodes/agent",
    )
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("running")
        await pilot.pause()
        row.set_state("completed", details="scored 8/10")
        await pilot.pause()
        assert "scored 8/10" in row.title
        assert "(agent)" not in row.title


@pytest.mark.asyncio
async def test_row_title_bare_when_node_type_none(
    mock_app_factory: Any,
) -> None:
    """Row without ``node_type`` renders no ``(...)`` suffix."""
    row = WorkflowNodeRow(node_name="untyped_node")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("running")
        await pilot.pause()
        assert "(" not in row.title
        assert ")" not in row.title


@pytest.mark.asyncio
async def test_counter_label_reads_events_not_turns(
    mock_app_factory: Any,
) -> None:
    """Trailing counter reads ``· 3 sub-agent events`` — not ``turns``."""
    row = WorkflowNodeRow(
        node_name="n",
        node_type="kolsch/nodes/agent",
    )
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        # bug-036d: any three sink calls bump the counter to 3.
        for i in range(3):
            await row.mount_assistant_message(f"m{i}", event_id=f"ev-{i}")
        await pilot.pause()
        assert "· 3 sub-agent events" in row.title
        assert "turns" not in row.title
