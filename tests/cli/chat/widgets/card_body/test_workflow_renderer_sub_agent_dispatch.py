"""spec-020 (bug-036d shape) — `WorkflowRenderer.apply_event` sub-agent dispatch.

bug-036d: the renderer no longer calls
``row.apply_sub_agent_event`` (deleted). Instead:

- With an adapter wired, ``_on_sub_agent_event`` reconstructs the
  inner event and hands it to
  :meth:`EventAdapter.dispatch_sub_agent_event`. The adapter's
  handler picks the row as sink via ``_route_to_workflow_node``
  and calls the row's typed ``mount_*`` method.
- Without an adapter, the renderer falls back to the row's sink
  methods directly.

Three passes:

1. Adapter path — the fan-out reaches the adapter's dispatch
   entrypoint with the reconstructed inner event.
2. Local fallback — the row's sink method fires.
3. Malformed / stale payloads (missing trace_id, unknown
   ``event_type``, no matching row) are dropped silently.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from cli.chat.widgets.card_body.workflow import (
    WorkflowRenderer,
    _ReconstructedSubAgentEvent,
)
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.events import EventType
from schemas.wire_kinds import WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


class _StubAdapter:
    """Records ``dispatch_sub_agent_event`` calls."""

    def __init__(self) -> None:
        self._live_nodes_by_trace_and_invocation: dict[
            tuple[str, str],
            WorkflowNodeRow,
        ] = {}
        self.dispatched: list[tuple[Any, str | None]] = []

    async def dispatch_sub_agent_event(
        self,
        inner_event: Any,
        *,
        outer_trace_id: str | None = None,
    ) -> None:
        self.dispatched.append((inner_event, outer_trace_id))


def _mk_sub_agent_payload(
    *,
    trace_id: str,
    invocation_id: str,
    node_name: str = "classify",
    event_type: str = "agent.message",
    payload: dict[str, Any] | None = None,
    run_id: str = "wrk_test",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "node_name": node_name,
        "invocation_id": invocation_id,
        "trace_id": trace_id,
        "event_type": event_type,
        "event_id": "ev_x",
        "payload": dict(payload or {}),
    }


@pytest.mark.asyncio
async def test_sub_agent_event_dispatched_via_adapter(
    mock_app_factory: Any,
) -> None:
    """Adapter path: renderer forwards the reconstructed event to
    ``EventAdapter.dispatch_sub_agent_event``."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        adapter = _StubAdapter()
        renderer.register_adapter(adapter)

        row = WorkflowNodeRow(node_name="classify", trace_id="tr_x")
        # bug-095: _node_rows is composite-keyed (name, invocation_id).
        renderer._node_rows[("classify", "inv_1")] = row
        adapter._live_nodes_by_trace_and_invocation[("tr_x", "inv_1")] = row

        payload = _mk_sub_agent_payload(
            trace_id="tr_x",
            invocation_id="inv_1",
            event_type="agent.message",
            payload={"content": {"text": "hello"}},
        )
        renderer.apply_event(
            _MockEvent(WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT, payload),
        )
        # The dispatch is scheduled as a task from the sync handler;
        # a pause lets the loop run it.
        await pilot.pause()

    assert len(adapter.dispatched) == 1
    evt, outer_trace = adapter.dispatched[0]
    assert isinstance(evt, _ReconstructedSubAgentEvent)
    assert evt.type == EventType.AGENT_MESSAGE
    assert evt.data == {"content": {"text": "hello"}}
    assert isinstance(evt.created_at, datetime)
    assert evt.trace_id == "tr_x"
    assert outer_trace == "tr_x"


@pytest.mark.asyncio
async def test_sub_agent_event_falls_back_to_row_sink(
    mock_app_factory: Any,
) -> None:
    """No adapter wired: renderer calls the row's sink method directly."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        row = WorkflowNodeRow(node_name="classify", trace_id="tr_x")
        # bug-095: composite key; the fallback routes on the sub-agent
        # envelope's (node_name, invocation_id) → ("classify", "inv_missing").
        renderer._node_rows[("classify", "inv_missing")] = row
        row.collapsed = False
        await pilot.pause()

        received: list[str] = []

        async def _spy(text: str, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            received.append(text)

        row.mount_assistant_message = _spy  # type: ignore[method-assign]

        payload = _mk_sub_agent_payload(
            trace_id="tr_x",
            invocation_id="inv_missing",
            event_type="agent.message",
            payload={"content": {"text": "hello"}},
        )
        renderer.apply_event(
            _MockEvent(WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT, payload),
        )
        # Fallback dispatch schedules the sink call as a task.
        await pilot.pause()
        await pilot.pause()

    assert received == ["hello"]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda p: p.pop("trace_id"),
            id="missing-trace-id",
        ),
        pytest.param(
            lambda p: p.pop("invocation_id"),
            id="missing-invocation-id",
        ),
        pytest.param(
            lambda p: p.__setitem__("event_type", "unknown.bogus"),
            id="unknown-event-type",
        ),
        pytest.param(
            lambda p: p.__setitem__("trace_id", ""),
            id="empty-trace-id",
        ),
    ],
)
@pytest.mark.asyncio
async def test_sub_agent_malformed_payloads_dropped(
    mock_app_factory: Any,
    mutate: Any,
) -> None:
    """Any envelope shape that can't route MUST NOT raise."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        adapter = _StubAdapter()
        renderer.register_adapter(adapter)
        row = WorkflowNodeRow(node_name="classify", trace_id="tr_x")
        # bug-095: composite key.
        renderer._node_rows[("classify", "inv_1")] = row
        adapter._live_nodes_by_trace_and_invocation[("tr_x", "inv_1")] = row

        payload = _mk_sub_agent_payload(
            trace_id="tr_x",
            invocation_id="inv_1",
        )
        mutate(payload)
        renderer.apply_event(
            _MockEvent(WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT, payload),
        )
        await pilot.pause()

    assert adapter.dispatched == []
