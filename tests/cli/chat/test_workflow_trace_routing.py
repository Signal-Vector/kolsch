"""spec-013 (bug-036d shape) — `EventAdapter` trace_id routing.

Three passes:

1. Register a live workflow node with trace_id=t1. Emit
   `agent.message` with trace_id=t1. Assert the row's sink method
   (``mount_assistant_message``) fires; no transcript mount.
2. Emit `agent.message` with trace_id=t-unrelated. Assert the
   transcript-root mount fires; no node dispatch.
3. Node terminal (`NODE_COMPLETED`) unregisters the row; a
   follow-up event with the same trace_id falls through to the
   transcript-root mount.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from workflows.events import WorkflowEventKind


class _FakeChatApp:
    def __init__(self) -> None:
        self.mounts: list[str] = []
        self._last_assistant: str = ""

    async def mount_user_message(
        self, text: str, timestamp: Any = None,
        *, event_id: Any = None, content: Any = None,
    ) -> None:
        _ = timestamp, event_id, content
        self.mounts.append(f"user:{text}")

    async def mount_assistant_message(
        self, text: str, *, event_id: Any = None,
    ) -> None:
        _ = event_id
        self.mounts.append(f"assistant:{text}")

    async def mount_tool_call(self, *args: Any, **kwargs: Any) -> None:
        _ = args, kwargs
        self.mounts.append("tool")

    async def set_turn_verb(self, verb: str, *, target: str = "") -> None:
        return None

    def last_assistant_text(self) -> str:
        return self._last_assistant

    def set_last_assistant_text(self, text: str) -> None:
        self._last_assistant = text


def _mk_agent_event(trace_id: str | None) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.AGENT_MESSAGE,
        data={"content": {"type": "text", "text": "hello"}},
        actor=Actor(kind="system"),
        created_at=datetime.now(tz=UTC),
        trace_id=trace_id,
    )


@pytest.mark.asyncio
async def test_matching_trace_id_routes_to_node() -> None:
    """Matched trace: sub-agent event dispatches to the node row.

    bug-033: composite key is `(trace_id, invocation_id)`; when the
    event's data dict doesn't carry `invocation_id`, the router
    falls back to a trace-only scan and still routes to the sole
    entry sharing that trace. Exercises that fallback here.
    """
    app = _FakeChatApp()
    adapter = EventAdapter(app)
    row = WorkflowNodeRow(node_name="n", trace_id="t1")
    adapter._live_nodes_by_trace_and_invocation[("t1", "inv_a")] = row

    event = _mk_agent_event(trace_id="t1")
    await adapter.handle(event)

    assert row._sub_agent_turn_count == 1
    # No transcript-root mount.
    assert not any(m.startswith("assistant:") for m in app.mounts)


@pytest.mark.asyncio
async def test_unrelated_trace_id_falls_through_to_transcript() -> None:
    """Unmatched trace: adapter defaults to transcript-root mount."""
    app = _FakeChatApp()
    adapter = EventAdapter(app)
    row = WorkflowNodeRow(node_name="n", trace_id="t1")
    adapter._live_nodes_by_trace_and_invocation[("t1", "inv_a")] = row

    event = _mk_agent_event(trace_id="t-unrelated")
    await adapter.handle(event)

    # Node not touched.
    assert row._sub_agent_turn_count == 0
    # Transcript mount fires.
    assert any(m.startswith("assistant:") for m in app.mounts)


@pytest.mark.asyncio
async def test_terminal_node_unregisters_from_live_map() -> None:
    """After NODE_COMPLETED the row leaves the trace map."""
    app = _FakeChatApp()
    adapter = EventAdapter(app)

    renderer = WorkflowRenderer()
    renderer.register_adapter(adapter)

    # RUN_STARTED anchor + NODE_STARTED wires the row.
    start_payload = {
        "run_id": "wrk_test",
        "workflow_ref": "kolsch/workflows/x",
        "kind": WorkflowEventKind.RUN_STARTED.value,
        "event_id": "ev_1",
        "ts": datetime.now(tz=UTC).isoformat(),
        "trace_id": "tr_run",
        "payload": {},
    }
    node_start_payload = {
        "run_id": "wrk_test",
        "workflow_ref": "kolsch/workflows/x",
        "kind": WorkflowEventKind.NODE_STARTED.value,
        "event_id": "ev_2",
        "ts": datetime.now(tz=UTC).isoformat(),
        "trace_id": "tr_run",
        "node_name": "n",
        "invocation_id": "inv_1",
        "payload": {},
    }

    class _FakeEventData:
        def __init__(self, kind: str, payload: dict[str, Any]) -> None:
            self.kind = kind
            self.payload = payload

    renderer.apply_event(_FakeEventData("kolsch.workflow.progress", start_payload))
    renderer.apply_event(_FakeEventData("kolsch.workflow.progress", node_start_payload))

    assert ("tr_run", "inv_1") in adapter._live_nodes_by_trace_and_invocation

    node_complete_payload = dict(node_start_payload)
    node_complete_payload["kind"] = WorkflowEventKind.NODE_COMPLETED.value
    node_complete_payload["event_id"] = "ev_3"
    renderer.apply_event(_FakeEventData("kolsch.workflow.progress", node_complete_payload))

    # Cleanup: composite key evicted from the map.
    assert (
        ("tr_run", "inv_1")
        not in adapter._live_nodes_by_trace_and_invocation
    )

    # Follow-up event with the same trace falls through.
    event = _mk_agent_event(trace_id="tr_run")
    await adapter.handle(event)
    assert any(m.startswith("assistant:") for m in app.mounts)
