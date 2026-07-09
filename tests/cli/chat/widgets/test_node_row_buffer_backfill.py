"""spec-021 — `WorkflowNodeRow` event buffer + backfill on expand.

Five regressions guarding the fix for spec-020's Reviewer flag:
users who expand a row AFTER a sub-agent has already produced
events used to see nothing but the turn counter (spec-013's
Coordinator #3 lock dropped collapsed-row events on the floor).
spec-021 lifts the lock — a bounded ring buffer holds recent
events, and expanding replays them into the body.

1. `test_events_while_collapsed_land_in_buffer` — N events fired
   at a collapsed row → buffer size N, turn count N, no live mount.
2. `test_expand_replays_buffer_to_body` — same setup + expand →
   N `_mount_sub_agent_widget` calls fire in order.
3. `test_live_expanded_mounts_do_not_duplicate_on_recollapse` —
   events into an expanded row live-mount; a collapse -> expand
   cycle does NOT re-mount them (dedupe by `event_id`).
4. `test_buffer_overflow_drops_oldest` — 51 events fired collapsed
   → buffer holds 50; the first event is the one dropped.
5. `test_terminal_state_freezes_buffer` — events after a terminal
   `set_state` transition are dropped (no append, no count, no
   mount).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.widgets.workflow_node_row import (
    _EVENT_BUFFER_MAX,
    WorkflowNodeRow,
)
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId


def _mk_agent_event(text: str = "hi") -> Event:
    """Build a well-formed `AGENT_MESSAGE` Event.

    Each call generates a fresh `event_id` so the dedupe set in
    `WorkflowNodeRow._mounted_event_ids` has a distinct key per
    event — matches the real per-event uniqueness the wire path
    delivers.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.AGENT_MESSAGE,
        data={"content": {"type": "text", "text": text}},
        actor=Actor(kind="system"),
        created_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_events_while_collapsed_land_in_buffer(
    fake_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec-021 AC #2 + AC #3 — collapsed row buffers without mounting.

    Fires three events at a fresh (collapsed) row and asserts:
    - `_event_buffer` holds all three in order (AC #2).
    - `_sub_agent_turn_count` reflects the count.
    - `_mount_sub_agent_widget` is NOT called (AC #3 — no live mount
      while collapsed; the parent's Coordinator #3 no-mount behavior
      is preserved for the collapsed case).
    """
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert row.collapsed is True

        mount_calls: list[Any] = []
        monkeypatch.setattr(
            row,
            "_mount_sub_agent_widget",
            lambda ev: mount_calls.append(ev),
        )

        events = [_mk_agent_event(f"m{i}") for i in range(3)]
        for event in events:
            row.apply_sub_agent_event(event)
        await pilot.pause()

        assert list(row._event_buffer) == events
        assert row._sub_agent_turn_count == 3
        assert mount_calls == []
        assert "3 sub-agent turns" in row.title


@pytest.mark.asyncio
async def test_expand_replays_buffer_to_body(
    fake_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec-021 AC #4 — collapsed -> expanded drains the buffer.

    Fires N events at a collapsed row (nothing mounts), then flips
    `collapsed = False`. The `watch_collapsed` reactive watcher
    fires `_replay_buffer_to_body`, which mounts each buffered
    event in order.
    """
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        mount_calls: list[Any] = []
        monkeypatch.setattr(
            row,
            "_mount_sub_agent_widget",
            lambda ev: mount_calls.append(ev),
        )

        # Buffer up while collapsed — no mount fires.
        events = [_mk_agent_event(f"m{i}") for i in range(4)]
        for event in events:
            row.apply_sub_agent_event(event)
        await pilot.pause()
        assert mount_calls == []

        # Expand — watch_collapsed replays the buffer in order.
        row.collapsed = False
        await pilot.pause()

        assert mount_calls == events


@pytest.mark.asyncio
async def test_live_expanded_mounts_do_not_duplicate_on_recollapse(
    fake_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec-021 AC #5 — collapse -> re-expand cycle doesn't duplicate.

    Row starts expanded. Events arrive → live-mount (each stamps
    its dedupe key into `_mounted_event_ids`). Collapse → expand
    again: `_replay_buffer_to_body` walks the buffer but skips
    every event whose key is already stamped, so no widget is
    mounted twice.

    This is the "prefer (b)" semantic from spec-021 Q3 — track
    already-mounted event_ids and skip on replay.
    """
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        mount_calls: list[Any] = []
        # Wrap the real method so `_mounted_event_ids` still gets
        # stamped — we're testing the dedupe behavior, not the mount.
        real_mount = row._mount_sub_agent_widget

        def _spy(ev: Any) -> None:
            mount_calls.append(ev)
            real_mount(ev)

        monkeypatch.setattr(row, "_mount_sub_agent_widget", _spy)

        # Start expanded — events live-mount.
        row.collapsed = False
        await pilot.pause()

        events = [_mk_agent_event(f"m{i}") for i in range(3)]
        for event in events:
            row.apply_sub_agent_event(event)
        await pilot.pause()
        assert mount_calls == events

        # Collapse — no additional mounts.
        row.collapsed = True
        await pilot.pause()
        assert mount_calls == events

        # Re-expand — replay walks the buffer but the dedupe guard
        # skips every event that already stamped its key.
        row.collapsed = False
        await pilot.pause()

        # mount_calls unchanged — no duplicate.
        assert mount_calls == events


@pytest.mark.asyncio
async def test_buffer_overflow_drops_oldest(
    fake_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec-021 AC #6 — 51st event drops the oldest.

    `_event_buffer` is `deque(maxlen=50)`; appending past capacity
    evicts the leftmost element. We fire `_EVENT_BUFFER_MAX + 1`
    events at a collapsed row and assert:
    - buffer length is exactly `_EVENT_BUFFER_MAX` (50).
    - the first event is NOT in the buffer.
    - the last event IS the buffer's tail.
    """
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Suppress mount side-effects — we're testing buffer sizing.
        monkeypatch.setattr(row, "_mount_sub_agent_widget", lambda _ev: None)

        events = [_mk_agent_event(f"m{i}") for i in range(_EVENT_BUFFER_MAX + 1)]
        for event in events:
            row.apply_sub_agent_event(event)
        await pilot.pause()

        assert len(row._event_buffer) == _EVENT_BUFFER_MAX
        # Oldest dropped.
        assert events[0] not in row._event_buffer
        # Newest retained at the tail.
        assert row._event_buffer[-1] is events[-1]
        # Turn count is NOT capped — it reflects total arrivals so the
        # collapsed title stays truthful about how much happened.
        assert row._sub_agent_turn_count == _EVENT_BUFFER_MAX + 1


@pytest.mark.asyncio
async def test_terminal_state_freezes_buffer(
    fake_app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec-021 AC #7 — terminal transition freezes the buffer.

    Fires two events (row alive), transitions to `completed`, then
    fires two more. The post-terminal events must be dropped:
    - `_event_buffer` still holds only the first two.
    - `_sub_agent_turn_count` stops at 2.
    - `_mount_sub_agent_widget` fires only for the pre-terminal pair
      (and only when the row happens to be expanded — here we start
      collapsed so no mounts fire at all before the terminal cut).
    """
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        mount_calls: list[Any] = []
        monkeypatch.setattr(
            row,
            "_mount_sub_agent_widget",
            lambda ev: mount_calls.append(ev),
        )

        pre = [_mk_agent_event(f"pre{i}") for i in range(2)]
        for event in pre:
            row.apply_sub_agent_event(event)
        await pilot.pause()

        assert list(row._event_buffer) == pre
        assert row._sub_agent_turn_count == 2

        # Terminal transition — watch_state flips the ``_buffer_frozen``
        # latch. mypy strict can't see the reactive side effect (the
        # write happens in ``watch_state`` via the ``state`` descriptor),
        # so we assert the observable consequences below rather than the
        # latch itself.
        row.set_state("completed", details="done")
        await pilot.pause()

        # Post-terminal events are dropped on the floor.
        post = [_mk_agent_event(f"post{i}") for i in range(2)]
        for event in post:
            row.apply_sub_agent_event(event)
        await pilot.pause()

        assert list(row._event_buffer) == pre
        assert row._sub_agent_turn_count == 2
        # Row started collapsed → no live mounts fired for either pair.
        assert mount_calls == []
