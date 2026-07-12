"""spec-013 Test 1 — `WorkflowRenderer` state transitions.

Five passes:

1. Mount `WorkflowRenderer` in a bare host app; feed `RUN_STARTED`,
   `NODE_STARTED(a)`, `NODE_COMPLETED(a)`, `RUN_COMPLETED`. Assert
   one `WorkflowNodeRow` mounted with node name "a", ends in
   `completed` state with glyph `✓`, header shows `[1/1]`, footer
   shows `✓ done in Ns`.
2. `NODE_FAILED(b, error_message="oops")` — row title contains
   truncated error preview.
3. `NODE_PRUNED(c)` — row state → `pruned`, glyph `─`.
4. `RUN_FAILED` — footer shows `✗ done in Ns`.
5. `RUN_ABORTED` — footer shows `✗ done in Ns` (cancelled).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from workflows.events import WorkflowEventKind


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


_WIRE = "kolsch.workflow.progress"


def _mk_payload(
    workflow_event_kind: WorkflowEventKind,
    *,
    node_name: str | None = None,
    trace_id: str = "tr_test",
    run_id: str = "wrk_test",
    ts: datetime | None = None,
    error_message: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Match the WIRE_WORKFLOW_PROGRESS payload shape."""
    ts_val = ts or datetime(2026, 7, 5, tzinfo=UTC)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "workflow_ref": "kolsch/workflows/test",
        "kind": workflow_event_kind.value,
        "event_id": "ev_test",
        "ts": ts_val.isoformat(),
        "trace_id": trace_id,
        "payload": {},
    }
    if node_name is not None:
        payload["node_name"] = node_name
        payload["invocation_id"] = f"inv_{node_name}"
    if error_message is not None:
        payload["payload"] = {"error_message": error_message}
    if summary is not None:
        payload["payload"] = {"summary": summary}
    return payload


@pytest.mark.asyncio
async def test_run_start_node_complete_run_complete_flow(mock_app_factory) -> None:
    """Happy-path: RUN_STARTED → NODE_STARTED(a) → NODE_COMPLETED(a) → RUN_COMPLETED."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        start_ts = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
        renderer.apply_event(_MockEvent(
            _WIRE,
            _mk_payload(WorkflowEventKind.RUN_STARTED, ts=start_ts),
        ))
        await pilot.pause()

        renderer.apply_event(_MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.NODE_STARTED,
                node_name="a",
                ts=start_ts,
            ),
        ))
        await pilot.pause()

        rows = list(renderer.query(WorkflowNodeRow))
        assert len(rows) == 1
        assert rows[0]._node_name == "a"
        assert rows[0].state == "starting"

        renderer.apply_event(_MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.NODE_COMPLETED,
                node_name="a",
                summary="ok",
                ts=start_ts,
            ),
        ))
        await pilot.pause()

        assert rows[0].state == "completed"
        assert "✓" in rows[0].title
        # Header shows [1/1]
        header_render = str(renderer._header.render())
        assert "[1/1]" in header_render

        end_ts = datetime(2026, 7, 5, 12, 0, 3, tzinfo=UTC)
        renderer.apply_event(_MockEvent(
            _WIRE,
            _mk_payload(WorkflowEventKind.RUN_COMPLETED, ts=end_ts),
        ))
        await pilot.pause()

        footer_render = str(renderer._footer.render())
        assert footer_render.startswith("✓ done in ")
        assert "s" in footer_render


@pytest.mark.asyncio
async def test_node_failed_shows_truncated_error(mock_app_factory) -> None:
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(_MockEvent(
            _WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED),
        ))
        renderer.apply_event(_MockEvent(
            _WIRE,
            _mk_payload(WorkflowEventKind.NODE_STARTED, node_name="b"),
        ))
        await pilot.pause()

        long_err = "oops-" + "x" * 200
        renderer.apply_event(_MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.NODE_FAILED,
                node_name="b",
                error_message=long_err,
            ),
        ))
        await pilot.pause()

        row = renderer._node_rows["b"]
        assert row.state == "failed"
        # "oops" prefix survives truncation.
        assert "oops" in row.title
        # Truncated: no full 200-char string in the title.
        assert long_err not in row.title


@pytest.mark.asyncio
async def test_node_pruned_transitions_to_pruned(mock_app_factory) -> None:
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(_MockEvent(
            _WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED),
        ))
        renderer.apply_event(_MockEvent(
            _WIRE,
            _mk_payload(WorkflowEventKind.NODE_STARTED, node_name="c"),
        ))
        renderer.apply_event(_MockEvent(
            _WIRE,
            _mk_payload(WorkflowEventKind.NODE_PRUNED, node_name="c"),
        ))
        await pilot.pause()
        row = renderer._node_rows["c"]
        assert row.state == "pruned"
        assert "─" in row.title


@pytest.mark.asyncio
async def test_run_failed_footer(mock_app_factory) -> None:
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(_MockEvent(
            _WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED),
        ))
        renderer.apply_event(_MockEvent(
            _WIRE, _mk_payload(WorkflowEventKind.RUN_FAILED),
        ))
        await pilot.pause()
        footer_render = str(renderer._footer.render())
        assert footer_render.startswith("✗ done in ")


@pytest.mark.asyncio
async def test_run_aborted_footer(mock_app_factory) -> None:
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(_MockEvent(
            _WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED),
        ))
        renderer.apply_event(_MockEvent(
            _WIRE, _mk_payload(WorkflowEventKind.RUN_ABORTED),
        ))
        await pilot.pause()
        footer_render = str(renderer._footer.render())
        assert footer_render.startswith("✗ done in ")
