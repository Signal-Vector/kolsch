"""spec-020 — `WorkflowRenderer.apply_event` sub-agent dispatch.

Isolated tests for the new dispatch case in
`WorkflowRenderer.apply_event` handling
`kolsch.workflow.node.sub_agent_event`. The renderer resolves the
target `WorkflowNodeRow` via bug-033's
`(trace_id, invocation_id)` composite map (or the local
`_node_rows` fallback for adapter-less tests) and hands a
reconstructed event to `row.apply_sub_agent_event(event)`.

Three passes:

1. Adapter path — a sub-agent UI event routes into the row that was
   registered under the matching `(trace_id, invocation_id)`.
2. Local fallback — with no adapter wired, the renderer resolves by
   `node_name` on `_node_rows`.
3. Malformed / stale payloads (missing trace_id, unknown
   `event_type`, no matching row) are dropped silently.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cli.chat.widgets.card_body.workflow import (
    WorkflowRenderer,
    _ReconstructedSubAgentEvent,
)
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.events import EventType
from schemas.wire_kinds import WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT


class _FakeEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


class _StubAdapter:
    """Minimal `EventAdapter` stand-in for the routing map."""

    def __init__(self) -> None:
        self._live_nodes_by_trace_and_invocation: dict[
            tuple[str, str], WorkflowNodeRow,
        ] = {}


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


# ---------------------------------------------------------------------------
# Test 1 — Adapter path routes to the registered row.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_event_routes_via_adapter_map(
    fake_app_factory: Any,
) -> None:
    """When the adapter's map has `(trace_id, invocation_id) → row`,
    the sub-agent event fires `row.apply_sub_agent_event`."""
    renderer = WorkflowRenderer()
    app = fake_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        adapter = _StubAdapter()
        renderer.register_adapter(adapter)

        row = WorkflowNodeRow(node_name="classify", trace_id="tr_x")
        renderer._node_rows["classify"] = row
        adapter._live_nodes_by_trace_and_invocation[("tr_x", "inv_1")] = row

        # Capture calls to the row's dispatch.
        received: list[Any] = []
        row.apply_sub_agent_event = received.append  # type: ignore[method-assign]

        payload = _mk_sub_agent_payload(
            trace_id="tr_x",
            invocation_id="inv_1",
            event_type="agent.message",
            payload={"content": {"text": "hello"}},
        )
        renderer.apply_event(
            _FakeEvent(WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT, payload),
        )
        await pilot.pause()

    assert len(received) == 1
    evt = received[0]
    assert isinstance(evt, _ReconstructedSubAgentEvent)
    assert evt.type == EventType.AGENT_MESSAGE
    assert evt.data == {"content": {"text": "hello"}}
    assert isinstance(evt.created_at, datetime)
    assert evt.trace_id == "tr_x"


# ---------------------------------------------------------------------------
# Test 2 — Local fallback when no adapter wired.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_event_falls_back_to_local_rows(
    fake_app_factory: Any,
) -> None:
    """Adapter-less rendering (headless) resolves the row via
    `_node_rows[node_name]` — the envelope carries `node_name`."""
    renderer = WorkflowRenderer()
    app = fake_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        row = WorkflowNodeRow(node_name="classify", trace_id="tr_x")
        renderer._node_rows["classify"] = row

        received: list[Any] = []
        row.apply_sub_agent_event = received.append  # type: ignore[method-assign]

        payload = _mk_sub_agent_payload(
            trace_id="tr_x",
            invocation_id="inv_missing",
            event_type="tool.called",
            payload={"tool_name": "bash"},
        )
        renderer.apply_event(
            _FakeEvent(WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT, payload),
        )
        await pilot.pause()

    assert len(received) == 1
    assert received[0].type == EventType.TOOL_CALLED


# ---------------------------------------------------------------------------
# Test 3 — Malformed / stale payloads are dropped.
# ---------------------------------------------------------------------------


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
    fake_app_factory: Any,
    mutate: Any,
) -> None:
    """Any envelope shape that can't route MUST NOT raise."""
    renderer = WorkflowRenderer()
    app = fake_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        row = WorkflowNodeRow(node_name="classify", trace_id="tr_x")
        renderer._node_rows["classify"] = row
        received: list[Any] = []
        row.apply_sub_agent_event = received.append  # type: ignore[method-assign]

        payload = _mk_sub_agent_payload(
            trace_id="tr_x",
            invocation_id="inv_1",
        )
        mutate(payload)
        # Must not raise.
        renderer.apply_event(
            _FakeEvent(WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT, payload),
        )
        await pilot.pause()

    assert received == []
