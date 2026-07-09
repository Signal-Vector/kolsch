"""bug-033 regression — sibling workflow nodes share `trace_id`.

Pre-bug-033 the `EventAdapter._live_nodes_by_trace_id` map was
keyed on `trace_id` alone. But `trace_id` is run-wide (see
`src/workflows/executor.py:1321-1328` and
`src/schemas/events.py`), so two sibling nodes in the same
workflow run both landed on the same map key: the second
`NODE_STARTED` clobbered the first's registration, and a
terminal event for one node evicted the other. Sub-agent
`agent.message` / `tool.called` / etc. events routed to
whichever sibling won the race — often the wrong one.

bug-033 re-keys the map on `(trace_id, invocation_id)` — an
executor-minted per-node id (hoisted at the UI event envelope
top level by `WorkflowsImpl._drain_workflow_bridge:1050-1052`).
This module regresses the four failure modes:

1. Two `NODE_STARTED` events with the same `trace_id` +
   distinct `invocation_id` → two map entries, both live.
2. `NODE_COMPLETED` for node A pops only A's entry; B stays.
3. A sub-agent-shaped event tagged with A's
   `(trace_id, invocation_id)` routes to A's row, not B's.
4. `NODE_STARTED` without an `invocation_id` falls back to the
   sentinel key AND emits a WARNING log — no crash.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from workflows.events import WorkflowEventKind


_WIRE = "kolsch.workflow.progress"


class _FakeChatApp:
    """Minimal `ChatApp`-shaped stub for the adapter constructor.

    Records mount calls so tests can assert transcript-root
    fall-through (or the absence thereof).
    """

    def __init__(self) -> None:
        self.mounts: list[str] = []
        self._last_assistant: str = ""

    async def mount_user_message(self, text: str, timestamp: Any) -> None:
        self.mounts.append(f"user:{text}")

    async def mount_assistant_message(self, text: str) -> None:
        self.mounts.append(f"assistant:{text}")

    async def mount_tool_call(self, *args: Any, **kwargs: Any) -> None:
        self.mounts.append("tool")

    async def set_turn_verb(self, verb: str, *, target: str = "") -> None:
        return None

    def last_assistant_text(self) -> str:
        return self._last_assistant

    def set_last_assistant_text(self, text: str) -> None:
        self._last_assistant = text


class _FakeEventData:
    """`kolsch.workflow.progress` envelope shape the renderer reads."""

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


def _mk_node_started_payload(
    *,
    node_name: str,
    trace_id: str,
    invocation_id: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "wrk_collision",
        "workflow_ref": "kolsch/workflows/collision",
        "kind": WorkflowEventKind.NODE_STARTED.value,
        "event_id": f"ev_{uuid4()}",
        "ts": datetime.now(tz=UTC).isoformat(),
        "trace_id": trace_id,
        "node_name": node_name,
        "payload": {},
    }
    if invocation_id is not None:
        payload["invocation_id"] = invocation_id
    return payload


def _mk_node_completed_payload(
    *,
    node_name: str,
    trace_id: str,
    invocation_id: str,
) -> dict[str, Any]:
    return {
        "run_id": "wrk_collision",
        "workflow_ref": "kolsch/workflows/collision",
        "kind": WorkflowEventKind.NODE_COMPLETED.value,
        "event_id": f"ev_{uuid4()}",
        "ts": datetime.now(tz=UTC).isoformat(),
        "trace_id": trace_id,
        "node_name": node_name,
        "invocation_id": invocation_id,
        "payload": {},
    }


def _mk_agent_event(
    *,
    trace_id: str,
    invocation_id: str | None,
) -> Event:
    data: dict[str, Any] = {"content": {"type": "text", "text": "hi"}}
    if invocation_id is not None:
        data["invocation_id"] = invocation_id
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.AGENT_MESSAGE,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(tz=UTC),
        trace_id=trace_id,
    )


def test_two_nodes_same_trace_distinct_invocation_register_separately() -> None:
    """AC #1 + #5 (part 1): sibling nodes get distinct map entries.

    Pre-bug-033 the second `NODE_STARTED` overwrote the first's
    registration — `assert len(live_map) == 2` would have read
    `1`. The composite key makes both entries survive.
    """
    app = _FakeChatApp()
    adapter = EventAdapter(app)
    renderer = WorkflowRenderer()
    renderer.register_adapter(adapter)

    renderer.apply_event(_FakeEventData(
        _WIRE,
        _mk_node_started_payload(
            node_name="alpha",
            trace_id="tr_1",
            invocation_id="inv_A",
        ),
    ))
    renderer.apply_event(_FakeEventData(
        _WIRE,
        _mk_node_started_payload(
            node_name="beta",
            trace_id="tr_1",
            invocation_id="inv_B",
        ),
    ))

    live_map = adapter._live_nodes_by_trace_and_invocation
    assert ("tr_1", "inv_A") in live_map
    assert ("tr_1", "inv_B") in live_map
    assert len(live_map) == 2
    # Rows are distinct instances.
    assert live_map[("tr_1", "inv_A")] is not live_map[("tr_1", "inv_B")]


def test_terminal_for_one_node_does_not_evict_sibling() -> None:
    """AC #3 + #5 (part 2): NODE_COMPLETED pops only the target key.

    Pre-bug-033 the unwriter blindly popped `trace_id` — so
    completing node A evicted node B's registration too. With the
    composite key, only A's entry disappears.
    """
    app = _FakeChatApp()
    adapter = EventAdapter(app)
    renderer = WorkflowRenderer()
    renderer.register_adapter(adapter)

    renderer.apply_event(_FakeEventData(
        _WIRE,
        _mk_node_started_payload(
            node_name="alpha",
            trace_id="tr_1",
            invocation_id="inv_A",
        ),
    ))
    renderer.apply_event(_FakeEventData(
        _WIRE,
        _mk_node_started_payload(
            node_name="beta",
            trace_id="tr_1",
            invocation_id="inv_B",
        ),
    ))
    renderer.apply_event(_FakeEventData(
        _WIRE,
        _mk_node_completed_payload(
            node_name="alpha",
            trace_id="tr_1",
            invocation_id="inv_A",
        ),
    ))

    live_map = adapter._live_nodes_by_trace_and_invocation
    assert ("tr_1", "inv_A") not in live_map
    assert ("tr_1", "inv_B") in live_map


@pytest.mark.asyncio
async def test_route_finds_correct_row_by_invocation_id() -> None:
    """AC #4 + #5 (part 3): the composite key routes to the right row.

    Registers alpha (`inv_A`) + beta (`inv_B`) sharing `tr_1`;
    fires a sub-agent event whose `data` dict carries
    `invocation_id="inv_A"`. Asserts alpha's row consumed the
    event and beta's row didn't — via `apply_sub_agent_event`'s
    turn counter on each row.
    """
    app = _FakeChatApp()
    adapter = EventAdapter(app)
    renderer = WorkflowRenderer()
    renderer.register_adapter(adapter)

    renderer.apply_event(_FakeEventData(
        _WIRE,
        _mk_node_started_payload(
            node_name="alpha",
            trace_id="tr_1",
            invocation_id="inv_A",
        ),
    ))
    renderer.apply_event(_FakeEventData(
        _WIRE,
        _mk_node_started_payload(
            node_name="beta",
            trace_id="tr_1",
            invocation_id="inv_B",
        ),
    ))

    row_alpha = adapter._live_nodes_by_trace_and_invocation[("tr_1", "inv_A")]
    row_beta = adapter._live_nodes_by_trace_and_invocation[("tr_1", "inv_B")]
    # Spy: replace `apply_sub_agent_event` on both rows with a
    # counter so we can prove which one received the event without
    # standing up a full CardWidget.
    hits: dict[str, int] = {"alpha": 0, "beta": 0}

    def _spy_alpha(event: Any) -> None:
        _ = event
        hits["alpha"] += 1

    def _spy_beta(event: Any) -> None:
        _ = event
        hits["beta"] += 1

    row_alpha.apply_sub_agent_event = _spy_alpha
    row_beta.apply_sub_agent_event = _spy_beta

    event = _mk_agent_event(trace_id="tr_1", invocation_id="inv_A")
    await adapter.handle(event)

    assert hits["alpha"] == 1
    assert hits["beta"] == 0


def test_missing_invocation_id_falls_back_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC #2 + #5 (part 4): missing `invocation_id` → sentinel + WARNING.

    Older payload shapes (pre-`invocation_id` hoist) or replay of
    truncated data may deliver `NODE_STARTED` without the field.
    The register path should not crash — it falls back to the
    sentinel `""` key and logs at WARNING.
    """
    app = _FakeChatApp()
    adapter = EventAdapter(app)
    renderer = WorkflowRenderer()
    renderer.register_adapter(adapter)

    with caplog.at_level(
        logging.WARNING,
        logger="cli.chat.widgets.card_body.workflow",
    ):
        renderer.apply_event(_FakeEventData(
            _WIRE,
            _mk_node_started_payload(
                node_name="alpha",
                trace_id="tr_1",
                invocation_id=None,
            ),
        ))

    live_map = adapter._live_nodes_by_trace_and_invocation
    assert ("tr_1", "") in live_map
    assert any(
        "missing invocation_id" in record.message
        and record.levelno == logging.WARNING
        for record in caplog.records
    )
