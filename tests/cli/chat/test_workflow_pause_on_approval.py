"""spec-014 Test 4 — `WorkflowNodeRow` paused/running on approval events.

Three passes:

1. `WorkflowRenderer` receives `NODE_STARTED("n1")`. Dispatch
   `kolsch.workflow.node.awaiting_approval` → row("n1").state ==
   "paused", glyph `⚠`.
2. Dispatch `kolsch.workflow.node.approval_resolved` → row("n1").state
   == "running", glyph `▸`.
3. An unrelated node's row (`n2`) is unaffected by the pause event
   targeting `n1`.

Tests exercise the renderer surface directly (not through
`EventAdapter.handle`) — the adapter fan-out is covered by Test 6.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cli.chat.state_tokens import resolve_state
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from workflows.events import WorkflowEventKind


def _mk_progress_payload(
    *,
    kind: str,
    run_id: str = "wrk_test",
    node_name: str | None = None,
    trace_id: str = "tr_root",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "workflow_ref": "kolsch/workflows/test",
        "kind": kind,
        "event_id": f"ev_{kind}",
        "ts": datetime.now(tz=UTC).isoformat(),
        "trace_id": trace_id,
        "payload": {},
    }
    if node_name is not None:
        payload["node_name"] = node_name
    return payload


def _mk_progress_event(payload: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "kolsch.workflow.progress", "payload": payload}


def _mk_approval_event(
    *,
    kind: str,
    run_id: str,
    node_name: str,
    resolution: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"run_id": run_id, "node_name": node_name}
    if resolution is not None:
        payload["resolution"] = resolution
    return {"kind": kind, "payload": payload}


@pytest.mark.asyncio
async def test_node_row_paused_on_awaiting_approval() -> None:
    """Dispatched WIRE_WORKFLOW_NODE_AWAITING_APPROVAL → row.state='paused'."""
    renderer = WorkflowRenderer()
    # Anchor via RUN_STARTED and land NODE_STARTED("n1").
    renderer.apply_event(
        _mk_progress_event(
            _mk_progress_payload(kind=WorkflowEventKind.RUN_STARTED.value),
        )
    )
    renderer.apply_event(
        _mk_progress_event(
            _mk_progress_payload(
                kind=WorkflowEventKind.NODE_STARTED.value,
                node_name="n1",
            ),
        )
    )
    row = renderer._node_rows["n1"]
    assert row.state == "running"

    # Dispatch the awaiting_approval event.
    renderer.apply_event(
        _mk_approval_event(
            kind="kolsch.workflow.node.awaiting_approval",
            run_id="wrk_test",
            node_name="n1",
        )
    )
    assert row.state == "paused"
    # Glyph verified via canonical STATE_TOKENS table (qol-075).
    assert resolve_state(row.state).glyph == "⚠"


@pytest.mark.asyncio
async def test_node_row_running_on_approval_resolved() -> None:
    """Dispatched WIRE_WORKFLOW_NODE_APPROVAL_RESOLVED → row.state='running'."""
    renderer = WorkflowRenderer()
    renderer.apply_event(
        _mk_progress_event(
            _mk_progress_payload(kind=WorkflowEventKind.RUN_STARTED.value),
        )
    )
    renderer.apply_event(
        _mk_progress_event(
            _mk_progress_payload(
                kind=WorkflowEventKind.NODE_STARTED.value,
                node_name="n1",
            ),
        )
    )
    row = renderer._node_rows["n1"]

    # Pause first.
    renderer.apply_event(
        _mk_approval_event(
            kind="kolsch.workflow.node.awaiting_approval",
            run_id="wrk_test",
            node_name="n1",
        )
    )
    assert row.state == "paused"

    # Resolve — row transitions back to running.
    renderer.apply_event(
        _mk_approval_event(
            kind="kolsch.workflow.node.approval_resolved",
            run_id="wrk_test",
            node_name="n1",
            resolution="approved",
        )
    )
    assert row.state == "running"
    assert resolve_state(row.state).glyph == "▸"


@pytest.mark.asyncio
async def test_unrelated_node_unaffected() -> None:
    """Approval event for `n1` leaves `n2`'s row state untouched."""
    renderer = WorkflowRenderer()
    renderer.apply_event(
        _mk_progress_event(
            _mk_progress_payload(kind=WorkflowEventKind.RUN_STARTED.value),
        )
    )
    renderer.apply_event(
        _mk_progress_event(
            _mk_progress_payload(
                kind=WorkflowEventKind.NODE_STARTED.value,
                node_name="n1",
            ),
        )
    )
    renderer.apply_event(
        _mk_progress_event(
            _mk_progress_payload(
                kind=WorkflowEventKind.NODE_STARTED.value,
                node_name="n2",
            ),
        )
    )
    row_n1 = renderer._node_rows["n1"]
    row_n2 = renderer._node_rows["n2"]

    renderer.apply_event(
        _mk_approval_event(
            kind="kolsch.workflow.node.awaiting_approval",
            run_id="wrk_test",
            node_name="n1",
        )
    )
    assert row_n1.state == "paused"
    # n2 is still in its initial `starting` state.
    assert row_n2.state == "running"
