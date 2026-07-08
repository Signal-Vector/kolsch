"""spec-013 Test 3 — `WorkflowNodeRow` sub-agent turn count + collapsed mount.

Three passes:

1. Three `apply_sub_agent_event` calls → `_sub_agent_turn_count == 3`
   and title contains "3 sub-agent turns".
2. Row collapsed — sub-agent events only increment the counter (no
   body mount).
3. Row expanded — new sub-agent event triggers a
   `_mount_sub_agent_widget` invocation (verified via monkeypatch).
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
async def test_three_sub_agent_events_increment_count(fake_app_factory) -> None:
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(3):
            row.apply_sub_agent_event(_mk_agent_event())
        await pilot.pause()
        assert row._sub_agent_turn_count == 3
        assert "3 sub-agent turns" in row.title


@pytest.mark.asyncio
async def test_collapsed_row_does_not_mount_sub_agent(
    fake_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Row starts collapsed (Collapsible default in __init__).
        assert row.collapsed is True

        calls: list[Any] = []

        def _spy(event: Any) -> None:
            calls.append(event)

        monkeypatch.setattr(row, "_mount_sub_agent_widget", _spy)

        row.apply_sub_agent_event(_mk_agent_event())
        await pilot.pause()

        # Coordinator #3 lock: no mount on collapsed.
        assert calls == []
        # Counter still incremented.
        assert row._sub_agent_turn_count == 1


@pytest.mark.asyncio
async def test_expanded_row_mounts_sub_agent(
    fake_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.collapsed = False
        await pilot.pause()

        calls: list[Any] = []

        def _spy(event: Any) -> None:
            calls.append(event)

        monkeypatch.setattr(row, "_mount_sub_agent_widget", _spy)

        event = _mk_agent_event()
        row.apply_sub_agent_event(event)
        await pilot.pause()

        # Expanded — mount fires.
        assert calls == [event]
        assert row._sub_agent_turn_count == 1
