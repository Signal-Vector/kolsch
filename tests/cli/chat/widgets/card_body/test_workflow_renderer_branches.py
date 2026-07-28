"""Coverage-fill for `WorkflowRenderer` branch/edge paths.

Drives the renderer through paths the existing suite leaves cold:
config-preview routing for compute / map / nested-workflow node types,
the adapter-less sub-agent fallback for USER_MESSAGE / TOOL_CALLED /
TOOL_RESULT / lifecycle events, the malformed-payload no-op guards, the
approval-lifecycle miss branches, compact-mode toggling, the elapsed /
timestamp helpers, and the composite-key lookup fallbacks. Harness
mirrors ``test_workflow_renderer.py`` (``_MockEvent`` + ``_mk_payload``
+ ``mock_app_factory``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.widgets.card_body.workflow import (
    WorkflowRenderer,
    _ReconstructedSubAgentEvent,
    _swallow_task_exception,
)
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.events import EventType
from schemas.wire_kinds import WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT
from workflows.events import WorkflowEventKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget

_WIRE = "kolsch.workflow.progress"
_AWAITING = "kolsch.workflow.node.awaiting_approval"
_RESOLVED = "kolsch.workflow.node.approval_resolved"


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: Any) -> None:
        self.kind = kind
        self.payload = payload


def _mk_payload(
    workflow_event_kind: WorkflowEventKind,
    *,
    node_name: str | None = None,
    invocation_id: str | None = None,
    node_type: str | None = None,
    trace_id: str = "tr_test",
    run_id: str = "wrk_test",
    ts: datetime | None = None,
    inner: dict[str, Any] | None = None,
    top_level: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a WIRE_WORKFLOW_PROGRESS payload.

    ``inner`` merges into the nested domain body (``payload["payload"]``);
    ``top_level`` merges into the envelope. ``node_type`` lands nested.
    """
    ts_val = ts or datetime(2026, 7, 27, tzinfo=UTC)
    body: dict[str, Any] = dict(inner or {})
    if node_type is not None:
        body["type"] = node_type
    payload: dict[str, Any] = {
        "run_id": run_id,
        "workflow_ref": "kolsch/workflows/test",
        "kind": workflow_event_kind.value,
        "event_id": "ev_test",
        "ts": ts_val.isoformat(),
        "trace_id": trace_id,
        "payload": body,
    }
    if node_name is not None:
        payload["node_name"] = node_name
        payload["invocation_id"] = invocation_id or f"inv_{node_name}"
    if top_level:
        payload.update(top_level)
    return payload


def _mk_sub_agent_payload(
    *,
    trace_id: str = "tr_test",
    invocation_id: str,
    node_name: str = "classify",
    event_type: str,
    run_id: str = "wrk_test",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Match the WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT envelope shape."""
    return {
        "run_id": run_id,
        "node_name": node_name,
        "invocation_id": invocation_id,
        "trace_id": trace_id,
        "event_type": event_type,
        "event_id": "ev_sub",
        "payload": dict(payload or {}),
    }


async def _seed_node(
    renderer: WorkflowRenderer,
    pilot: Any,
    *,
    node_name: str = "classify",
    node_type: str | None = None,
    invocation_id: str | None = None,
    inner: dict[str, Any] | None = None,
) -> WorkflowNodeRow:
    """RUN_STARTED + NODE_STARTED so one live row exists; return it."""
    renderer.apply_event(_MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED)))
    renderer.apply_event(
        _MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.NODE_STARTED,
                node_name=node_name,
                node_type=node_type,
                invocation_id=invocation_id,
                inner=inner,
            ),
        )
    )
    await pilot.pause()
    return renderer._node_rows[(node_name, invocation_id or f"inv_{node_name}")]


# ---------------------------------------------------------------------------
# _swallow_task_exception — the fire-and-forget done-callback (lines 85-92)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swallow_task_exception_cancelled_is_noop() -> None:
    """A cancelled task returns early without touching ``.exception()``."""

    async def _never() -> None:
        await asyncio.sleep(3600)

    task: asyncio.Task[None] = asyncio.ensure_future(_never())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Must not raise even though a cancelled task's .exception() would.
    _swallow_task_exception(task)


@pytest.mark.asyncio
async def test_swallow_task_exception_logs_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task that raised is logged (exc pulled) rather than re-raised."""
    import logging

    async def _boom() -> None:
        msg = "kaboom"
        raise RuntimeError(msg)

    task: asyncio.Task[None] = asyncio.ensure_future(_boom())
    with pytest.raises(RuntimeError):
        await task
    with caplog.at_level(logging.WARNING):
        _swallow_task_exception(task)
    assert any("sub-agent dispatch task raised" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_swallow_task_exception_clean_completion() -> None:
    """A cleanly completed task is a no-op (exc is None)."""

    async def _ok() -> None:
        return None

    task: asyncio.Task[None] = asyncio.ensure_future(_ok())
    await task
    _swallow_task_exception(task)  # exc is None → no log, no raise.


# ---------------------------------------------------------------------------
# apply_event — malformed / non-dispatched envelopes (500, 517, 521)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_event_non_dict_payload_dropped(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A non-dict payload short-circuits (line 500) — no crash, no row."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(_MockEvent(_WIRE, "not-a-dict"))
        renderer.apply_event(_MockEvent(_WIRE, None))
        await pilot.pause()
        assert renderer._node_rows == {}


@pytest.mark.asyncio
async def test_apply_event_unknown_wire_kind_dropped(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A wire kind the renderer doesn't own is ignored (line 517)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent("kolsch.card.opened", {"card_id": "x"}),
        )
        await pilot.pause()
        assert renderer._node_rows == {}


@pytest.mark.asyncio
async def test_apply_event_non_string_inner_kind_dropped(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A progress envelope with a non-string inner ``kind`` drops (line 521)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, {"kind": 123, "payload": {"foo": "bar"}}),
        )
        await pilot.pause()
        assert renderer._node_rows == {}


@pytest.mark.asyncio
async def test_apply_event_reads_dict_envelope_kind(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A plain-dict envelope (no ``.kind`` attr) reads ``kind`` from the dict."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = _mk_payload(WorkflowEventKind.RUN_STARTED)
        # Wrap the wire kind into a bare dict (no _MockEvent attr access).
        renderer.apply_event({"kind": _WIRE, "payload": started})
        renderer.apply_event(
            {
                "kind": _WIRE,
                "payload": _mk_payload(WorkflowEventKind.NODE_STARTED, node_name="d"),
            }
        )
        await pilot.pause()
        assert renderer._node_rows[("d", "inv_d")].state == "running"


# ---------------------------------------------------------------------------
# _on_run_started — inputs summary + placeholder manifest guards (587-636)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_started_nested_inputs_summarised(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """RUN_STARTED with nested ``payload.inputs`` fills the header summary."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.RUN_STARTED,
                    inner={"inputs": {"url": "https://x", "n": 3}},
                ),
            )
        )
        await pilot.pause()
        header = str(renderer._header.render())
        assert "inputs:" in header
        assert "url" in header


@pytest.mark.asyncio
async def test_run_started_sibling_inputs_fallback(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """RUN_STARTED with a top-level ``inputs`` (no nested) is used (591-596)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        payload = _mk_payload(WorkflowEventKind.RUN_STARTED)
        payload["inputs"] = {"topkey": "topval"}
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        assert "topkey" in str(renderer._header.render())


@pytest.mark.asyncio
async def test_run_started_non_string_run_id_and_ref_ignored(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Non-string run_id / workflow_ref are not stored (566->570, 571->585)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        payload = _mk_payload(WorkflowEventKind.RUN_STARTED)
        payload["run_id"] = 999
        payload["workflow_ref"] = 12.5
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        assert renderer._run_id is None
        assert renderer._workflow_ref is None


@pytest.mark.asyncio
async def test_run_started_manifest_drops_malformed_entries(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Non-dict / bad-name / bad-type manifest entries are skipped (630-636)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.RUN_STARTED,
                    inner={
                        "nodes": [
                            "not-a-dict",
                            {"name": 5, "type": "kolsch/nodes/bash"},
                            {"name": "ok", "type": None},
                            {"name": "good", "type": "kolsch/nodes/bash"},
                            # Duplicate name → idempotent skip (line 636).
                            {"name": "good", "type": "kolsch/nodes/bash"},
                        ]
                    },
                ),
            )
        )
        await pilot.pause()
        # Only the one valid, unique entry mounts.
        assert set(renderer._node_rows) == {("good", "")}


# ---------------------------------------------------------------------------
# _route_config_preview — compute / map / nested-workflow (737-761)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_node_routes_value_preview(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A compute node's ``value`` preview reaches ``set_value_preview``."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    calls: list[tuple[str, str | None]] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_STARTED,
                    node_name="calc",
                    node_type="kolsch/nodes/compute",
                    inner={
                        "node_config_preview": {
                            "value": "{{ inputs.n }} + 1",
                            "rendered_value": "5 + 1",
                        },
                    },
                ),
            )
        )
        await pilot.pause()
        row = renderer._node_rows[("calc", "inv_calc")]
        orig = row.set_value_preview

        def _spy(value: str, *, rendered: str | None = None) -> None:
            calls.append((value, rendered))
            orig(value, rendered=rendered)

        # Re-route through a fresh event to exercise the setter dispatch.
        row.set_value_preview = _spy  # type: ignore[method-assign]
        renderer._route_config_preview(
            row,
            {"value": "{{ inputs.n }} + 1", "rendered_value": "5 + 1"},
        )
    assert calls == [("{{ inputs.n }} + 1", "5 + 1")]


@pytest.mark.asyncio
async def test_map_node_routes_over_preview() -> None:
    """A map node's ``over`` preview reaches ``set_over_preview``."""
    renderer = WorkflowRenderer()
    row = WorkflowNodeRow(node_name="fan", node_type="kolsch/nodes/map")
    calls: list[tuple[str, str | None]] = []

    def _spy(over: str, *, rendered: str | None = None) -> None:
        calls.append((over, rendered))

    row.set_over_preview = _spy  # type: ignore[method-assign]
    renderer._route_config_preview(
        row,
        {"over": "{{ inputs.items }}", "rendered_over": "[1, 2, 3]"},
    )
    assert calls == [("{{ inputs.items }}", "[1, 2, 3]")]


@pytest.mark.asyncio
async def test_workflow_node_routes_binds_preview() -> None:
    """A nested-workflow node routes ref/binds to ``set_workflow_binds_preview``."""
    renderer = WorkflowRenderer()
    row = WorkflowNodeRow(node_name="sub", node_type="kolsch/nodes/workflow")
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def _spy(
        ref: str,
        binds: dict[str, Any],
        rendered_binds: dict[str, Any],
    ) -> None:
        calls.append((ref, binds, rendered_binds))

    row.set_workflow_binds_preview = _spy  # type: ignore[method-assign]
    renderer._route_config_preview(
        row,
        {
            "ref": "b/workflow/child",
            "binds": {"x": "{{ inputs.x }}"},
            "rendered_binds": {"x": "42"},
        },
    )
    assert calls == [
        ("b/workflow/child", {"x": "{{ inputs.x }}"}, {"x": "42"}),
    ]


@pytest.mark.asyncio
async def test_workflow_node_binds_defaults_non_dict() -> None:
    """Non-dict binds / rendered_binds coerce to empty dicts."""
    renderer = WorkflowRenderer()
    row = WorkflowNodeRow(node_name="sub", node_type="kolsch/nodes/workflow")
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def _spy(
        ref: str,
        binds: dict[str, Any],
        rendered_binds: dict[str, Any],
    ) -> None:
        calls.append((ref, binds, rendered_binds))

    row.set_workflow_binds_preview = _spy  # type: ignore[method-assign]
    renderer._route_config_preview(
        row,
        {"ref": "b/workflow/child", "binds": "nope", "rendered_binds": 5},
    )
    assert calls == [("b/workflow/child", {}, {})]


@pytest.mark.asyncio
async def test_empty_config_preview_routes_nowhere() -> None:
    """An agent/noop preview (no recognised key) routes to no setter."""
    renderer = WorkflowRenderer()
    row = WorkflowNodeRow(node_name="think", node_type="kolsch/nodes/agent")
    # None of these setters should fire for an empty preview.
    fired: list[str] = []

    def _mk_spy(label: str) -> Callable[..., None]:
        def _spy(*a: Any, **k: Any) -> None:
            _ = a, k
            fired.append(label)

        return _spy

    row.set_command_preview = _mk_spy("command")  # type: ignore[method-assign]
    row.set_value_preview = _mk_spy("value")  # type: ignore[method-assign]
    row.set_over_preview = _mk_spy("over")  # type: ignore[method-assign]
    row.set_workflow_binds_preview = _mk_spy("binds")  # type: ignore[method-assign]
    renderer._route_config_preview(row, {})
    assert fired == []


# ---------------------------------------------------------------------------
# _on_node_started — bad name early-out (782) + top-level fields (806, 826)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_started_missing_name_dropped(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """NODE_STARTED with an empty node_name mounts nothing (line 782)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        payload = _mk_payload(WorkflowEventKind.NODE_STARTED)
        payload["node_name"] = ""
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        assert renderer._node_rows == {}


@pytest.mark.asyncio
async def test_node_started_reads_top_level_type_and_parent(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Old-shape top-level ``type`` / ``parent_invocation_id`` are read.

    Exercises the 803-806 (top-level type fallback) and 823-826
    (top-level parent_invocation_id fallback) branches. With no matching
    parent row, the iteration child mounts at the workflow level (895).
    """
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        payload = _mk_payload(
            WorkflowEventKind.NODE_STARTED,
            node_name="orphan_iter",
            invocation_id="inv_orphan",
        )
        # No nested type/parent — supply BOTH at the top level.
        payload["type"] = "kolsch/nodes/bash"
        payload["parent_invocation_id"] = "inv_missing_parent"
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        row = renderer._node_rows[("orphan_iter", "inv_orphan")]
        assert row._node_type == "kolsch/nodes/bash"
        # Parent not found → mounted at the workflow level as a fallback.
        top_level = list(renderer._node_list.query(WorkflowNodeRow))
        assert row in top_level


@pytest.mark.asyncio
async def test_node_started_replay_dedup_no_reset(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A replayed NODE_STARTED for a completed row does not reset it (832)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(renderer, pilot, node_name="once")
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(WorkflowEventKind.NODE_COMPLETED, node_name="once"),
            )
        )
        await pilot.pause()
        assert row.state == "success"
        # Replay the START — dedup guard hits, state stays success.
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(WorkflowEventKind.NODE_STARTED, node_name="once"),
            )
        )
        await pilot.pause()
        assert row.state == "success"


@pytest.mark.asyncio
async def test_node_started_on_decided_placeholder_is_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Adopt path: a placeholder already past ``pending`` short-circuits (866-868).

    A placeholder is pre-mounted under ``(name, "")``. NODE_PRUNED decides
    it in place (→ ``skipped``, still keyed at ``(name, "")``). A later
    NODE_STARTED carrying an invocation_id finds the placeholder NON-pending
    on the adopt branch → the else-branch returns without mounting a second
    row or resetting the skipped state.
    """
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.RUN_STARTED,
                    inner={
                        "nodes": [
                            {"name": "p", "type": "kolsch/nodes/bash"},
                        ]
                    },
                ),
            )
        )
        await pilot.pause()
        # Prune the placeholder in place → skipped, still at ("p", "").
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_PRUNED,
                    node_name="p",
                    invocation_id="inv_synthetic",
                ),
            )
        )
        await pilot.pause()
        placeholder = renderer._node_rows[("p", "")]
        assert placeholder.state == "skipped"

        # A NODE_STARTED with a real invocation_id: key ("p", "inv_p") is a
        # dedup MISS, so the adopt branch runs; the placeholder is non-pending
        # → else-branch return. No second row, skipped state preserved.
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(WorkflowEventKind.NODE_STARTED, node_name="p"),
            )
        )
        await pilot.pause()
        assert renderer._node_rows[("p", "")] is placeholder
        assert placeholder.state == "skipped"
        assert ("p", "inv_p") not in renderer._node_rows
        assert sum(1 for k in renderer._node_rows if k[0] == "p") == 1


# ---------------------------------------------------------------------------
# Per-kind handler no-op guards (913, 960, 999, 1031, 1055) — unknown name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        WorkflowEventKind.NODE_COMPLETED,
        WorkflowEventKind.NODE_FAILED,
        WorkflowEventKind.NODE_PRUNED,
        WorkflowEventKind.CALLED_WORKFLOW,
        WorkflowEventKind.CALLED_WORKFLOW_COMPLETED,
    ],
)
async def test_node_event_for_unknown_row_is_noop(
    mock_app_factory: Callable[[Widget], App[None]],
    kind: WorkflowEventKind,
) -> None:
    """A per-node event for a name with no row is a silent no-op."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        renderer.apply_event(_MockEvent(_WIRE, _mk_payload(kind, node_name="ghost")))
        await pilot.pause()
        assert renderer._node_rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        WorkflowEventKind.NODE_COMPLETED,
        WorkflowEventKind.NODE_FAILED,
        WorkflowEventKind.NODE_PRUNED,
        WorkflowEventKind.CALLED_WORKFLOW,
        WorkflowEventKind.CALLED_WORKFLOW_COMPLETED,
    ],
)
async def test_node_event_non_string_name_dropped(
    mock_app_factory: Callable[[Widget], App[None]],
    kind: WorkflowEventKind,
) -> None:
    """A non-string node_name short-circuits each handler."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        payload = _mk_payload(kind, node_name="x")
        payload["node_name"] = 123  # non-string
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        assert renderer._node_rows == {}


@pytest.mark.asyncio
async def test_node_failed_error_from_top_level(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """NODE_FAILED reads a top-level error field when inner has none (973-977)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(renderer, pilot, node_name="f")
        payload = _mk_payload(WorkflowEventKind.NODE_FAILED, node_name="f")
        # No nested error; hoist to the envelope top level.
        payload["error"] = "top-level boom"
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        assert row.state == "failed"


@pytest.mark.asyncio
async def test_node_completed_summary_from_top_level(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """NODE_COMPLETED falls back to a top-level ``summary`` (930-933)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(renderer, pilot, node_name="c")
        payload = _mk_payload(WorkflowEventKind.NODE_COMPLETED, node_name="c")
        payload["summary"] = "top summary"
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        assert row.state == "success"


@pytest.mark.asyncio
async def test_node_completed_reads_duration_ms(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """NODE_COMPLETED forwards a nested ``duration_ms`` (939-943)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="b", node_type="kolsch/nodes/bash"
        )
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_COMPLETED,
                    node_name="b",
                    inner={
                        "duration_ms": 1234.0,
                        "output": {"stdout": "ok", "exit_code": 0},
                    },
                ),
            )
        )
        await pilot.pause()
        assert row.state == "success"


@pytest.mark.asyncio
async def test_called_workflow_non_dict_inner_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """CALLED_WORKFLOW with a non-dict inner payload is a no-op (1037)."""
    from cli.chat.widgets.workflow_node_row import WorkflowCallLink

    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer,
            pilot,
            node_name="callw",
            node_type="kolsch/nodes/workflow",
        )
        payload = _mk_payload(WorkflowEventKind.CALLED_WORKFLOW, node_name="callw")
        payload["payload"] = "not-a-dict"
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        assert not list(row.query(WorkflowCallLink))


@pytest.mark.asyncio
async def test_called_workflow_missing_child_fields_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """CALLED_WORKFLOW without child run_id/ref mounts no link (1041)."""
    from cli.chat.widgets.workflow_node_row import WorkflowCallLink

    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer,
            pilot,
            node_name="callw",
            node_type="kolsch/nodes/workflow",
        )
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW,
                    node_name="callw",
                    inner={"child_workflow_ref": "only-ref"},
                ),
            )
        )
        await pilot.pause()
        assert not list(row.query(WorkflowCallLink))


@pytest.mark.asyncio
async def test_called_workflow_completed_non_dict_and_missing_status(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """CALLED_WORKFLOW_COMPLETED guards: non-dict inner (1061) + no status (1064)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_node(
            renderer,
            pilot,
            node_name="callw",
            node_type="kolsch/nodes/workflow",
        )
        # Non-dict inner → early return.
        bad = _mk_payload(
            WorkflowEventKind.CALLED_WORKFLOW_COMPLETED, node_name="callw"
        )
        bad["payload"] = 42
        renderer.apply_event(_MockEvent(_WIRE, bad))
        # Dict inner but no child_status → early return.
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW_COMPLETED,
                    node_name="callw",
                    inner={"other": "x"},
                ),
            )
        )
        await pilot.pause()  # no crash is the assertion


# ---------------------------------------------------------------------------
# Approval-lifecycle miss branches (1077, 1082, 1293, 1298)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_awaiting_approval_empty_name_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """An awaiting_approval with an empty node_name is a no-op (1077)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(renderer, pilot, node_name="n1")
        renderer.apply_event(_MockEvent(_AWAITING, {"run_id": "r", "node_name": ""}))
        await pilot.pause()
        assert row.state == "running"


@pytest.mark.asyncio
async def test_awaiting_approval_unknown_name_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """An awaiting_approval for a name with no row is a no-op (1082)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_node(renderer, pilot, node_name="n1")
        renderer.apply_event(
            _MockEvent(_AWAITING, {"run_id": "r", "node_name": "ghost"}),
        )
        await pilot.pause()
        assert renderer._node_rows[("n1", "inv_n1")].state == "running"


@pytest.mark.asyncio
async def test_approval_resolved_empty_name_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """An approval_resolved with an empty node_name is a no-op (1293)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(renderer, pilot, node_name="n1")
        # Pause first via a valid awaiting event.
        renderer.apply_event(_MockEvent(_AWAITING, {"run_id": "r", "node_name": "n1"}))
        await pilot.pause()
        assert row.state == "paused"
        # Empty-name resolve → no-op; still paused.
        renderer.apply_event(_MockEvent(_RESOLVED, {"run_id": "r", "node_name": ""}))
        await pilot.pause()
        assert row.state == "paused"


@pytest.mark.asyncio
async def test_approval_resolved_unknown_name_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """An approval_resolved for a name with no row is a no-op (1298)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(renderer, pilot, node_name="n1")
        renderer.apply_event(_MockEvent(_AWAITING, {"run_id": "r", "node_name": "n1"}))
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_RESOLVED, {"run_id": "r", "node_name": "ghost"}),
        )
        await pilot.pause()
        assert row.state == "paused"


# ---------------------------------------------------------------------------
# Adapter-less sub-agent fallback dispatch (_dispatch_reconstructed_to_row)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_user_message_mounts_text(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """USER_MESSAGE with text calls ``mount_user_message`` (1206-1213)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="classify", invocation_id="inv_1"
        )
        received: list[str] = []

        async def _spy(text: str, *a: Any, **k: Any) -> None:
            _ = a, k
            received.append(text)

        row.mount_user_message = _spy  # type: ignore[method-assign]
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    event_type="user.message",
                    payload={"content": {"text": "hi user"}},
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert received == ["hi user"]


@pytest.mark.asyncio
async def test_fallback_user_message_empty_counts_lifecycle(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A text-less USER_MESSAGE bumps the lifecycle counter (1215)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="classify", invocation_id="inv_1"
        )
        before = row._sub_agent_turn_count
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    event_type="user.message",
                    payload={"content": {}},
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert row._sub_agent_turn_count == before + 1


@pytest.mark.asyncio
async def test_fallback_user_message_extracts_non_text_content(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """USER_MESSAGE with a non-``text`` dict routes via _extract_content_text (1200)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="classify", invocation_id="inv_1"
        )
        received: list[str] = []

        async def _spy(text: str, *a: Any, **k: Any) -> None:
            _ = a, k
            received.append(text)

        row.mount_user_message = _spy  # type: ignore[method-assign]
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    event_type="user.message",
                    payload={"content": {"output": "extracted body"}},
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert received == ["extracted body"]


@pytest.mark.asyncio
async def test_fallback_agent_message_extracts_non_text_content(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """AGENT_MESSAGE with a non-``text`` dict routes via _extract (1223-1228)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="classify", invocation_id="inv_1"
        )
        received: list[str] = []

        async def _spy(text: str, *a: Any, **k: Any) -> None:
            _ = a, k
            received.append(text)

        row.mount_assistant_message = _spy  # type: ignore[method-assign]
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    event_type="agent.message",
                    payload={"content": {"output": "agent body"}},
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert received == ["agent body"]


@pytest.mark.asyncio
async def test_fallback_tool_called_mounts(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """TOOL_CALLED reaches ``mount_tool_call`` with args in the body (1264-1279)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="classify", invocation_id="inv_1"
        )
        seen: list[tuple[str, str, str]] = []

        async def _spy(
            tool_name: str,
            args_str: str,
            body: str,
            *a: Any,
            **k: Any,
        ) -> None:
            _ = a, k
            seen.append((tool_name, args_str, body))

        row.mount_tool_call = _spy  # type: ignore[method-assign]
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    event_type="tool.called",
                    payload={
                        "tool_name": "read_file",
                        "arguments": {"path": "/tmp/x"},
                    },
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert len(seen) == 1
    tool_name, args_str, body = seen[0]
    assert tool_name == "read_file"
    assert "path" in args_str
    assert "path" in body


@pytest.mark.asyncio
async def test_fallback_tool_result_mounts(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """TOOL_RESULT formats a dict result into the body (1253-1260)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="classify", invocation_id="inv_1"
        )
        seen: list[tuple[str, str, str]] = []

        async def _spy(
            tool_name: str,
            args_str: str,
            body: str,
            *a: Any,
            **k: Any,
        ) -> None:
            _ = a, k
            seen.append((tool_name, args_str, body))

        row.mount_tool_call = _spy  # type: ignore[method-assign]
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    event_type="tool.result",
                    payload={
                        "tool_name": "read_file",
                        "args": "raw-arg-string",
                        "result": {"content": "file body"},
                    },
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert len(seen) == 1
    assert seen[0][0] == "read_file"
    assert "raw-arg-string" in seen[0][1]
    assert seen[0][2] != ""


@pytest.mark.asyncio
async def test_fallback_unknown_event_type_counts_lifecycle(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A recognised-but-unhandled event bumps the lifecycle counter (1281)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="classify", invocation_id="inv_1"
        )
        before = row._sub_agent_turn_count
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    event_type="config.set",
                    payload={"key": "k", "value": "v"},
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert row._sub_agent_turn_count == before + 1


@pytest.mark.asyncio
async def test_fallback_agent_message_duplicate_counts_lifecycle(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A repeat AGENT_MESSAGE with identical text bumps the counter (1236)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="classify", invocation_id="inv_1"
        )
        row.set_last_assistant_text("same text")
        before = row._sub_agent_turn_count
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    event_type="agent.message",
                    payload={"content": {"text": "same text"}},
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert row._sub_agent_turn_count == before + 1


@pytest.mark.asyncio
async def test_fallback_routes_by_invocation_when_name_differs(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """No composite match → invocation-only fallback finds the row (1164)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = await _seed_node(
            renderer, pilot, node_name="realname", invocation_id="inv_1"
        )
        received: list[str] = []

        async def _spy(text: str, *a: Any, **k: Any) -> None:
            _ = a, k
            received.append(text)

        row.mount_user_message = _spy  # type: ignore[method-assign]
        # node_name in the envelope differs from the row's registered name,
        # so the composite key misses and the invocation-only match hits.
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_1",
                    node_name="different",
                    event_type="user.message",
                    payload={"content": {"text": "routed"}},
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()
    assert received == ["routed"]


@pytest.mark.asyncio
async def test_fallback_no_matching_row_is_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """A sub-agent event with no matching row anywhere is a no-op (1166)."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_node(renderer, pilot, node_name="classify", invocation_id="inv_1")
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    invocation_id="inv_nowhere",
                    node_name="classify",
                    event_type="user.message",
                    payload={"content": {"text": "lost"}},
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()  # no crash is the assertion


@pytest.mark.asyncio
async def test_dispatch_reconstructed_outside_loop_returns() -> None:
    """``_dispatch_reconstructed_to_row`` with no running loop returns (1185-1186)."""
    renderer = WorkflowRenderer()
    row = WorkflowNodeRow(node_name="x")
    event = _ReconstructedSubAgentEvent(
        type=EventType.USER_MESSAGE,
        data={"content": {"text": "hi"}},
        created_at=datetime.now(tz=UTC),
        trace_id="tr",
        event_id="ev",
    )
    # Called synchronously — no running event loop → early return, no raise.
    renderer._dispatch_reconstructed_to_row(row, event)


# ---------------------------------------------------------------------------
# Compact mode watcher off-branch + live-sync (1419-1422, 1398)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_mode_toggle_off_restores_full(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Flipping compact_mode on then off restores the node list + footer."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_node(renderer, pilot, node_name="a")

        renderer.compact_mode = True
        await pilot.pause()
        compact_display = {
            "list": renderer._node_list.display,
            "footer": renderer._footer.display,
            "compact_line": renderer._compact_line.display,
        }
        assert compact_display == {
            "list": False,
            "footer": False,
            "compact_line": True,
        }
        assert renderer.has_class("-compact")

        # A live update while compact keeps the summary line current (1398).
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(WorkflowEventKind.NODE_COMPLETED, node_name="a"),
            )
        )
        await pilot.pause()
        assert "1/1" in str(renderer._compact_line.render())

        # Flip back off — full render restored (1419-1422).
        renderer.compact_mode = False
        await pilot.pause()
        full_display = {
            "list": renderer._node_list.display,
            "footer": renderer._footer.display,
            "compact_line": renderer._compact_line.display,
        }
        assert full_display == {
            "list": True,
            "footer": True,
            "compact_line": False,
        }
        assert not renderer.has_class("-compact")


@pytest.mark.asyncio
async def test_compact_summary_falls_back_to_run_id(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """With no workflow_ref, the compact summary uses the run id."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        # RUN_STARTED payload with a run_id but NO workflow_ref.
        payload = _mk_payload(WorkflowEventKind.RUN_STARTED)
        del payload["workflow_ref"]
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        renderer.compact_mode = True
        await pilot.pause()
        summary = str(renderer._compact_line.render())
        assert "wrk_test" in summary


# ---------------------------------------------------------------------------
# Elapsed / timestamp helpers (1341-1364, 1453, 1457)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_elapsed_derived_from_timestamps(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Terminal footer derives elapsed from ts delta when none is explicit."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    start = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
    end = datetime(2026, 7, 27, 12, 0, 5, tzinfo=UTC)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED, ts=start))
        )
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_COMPLETED, ts=end))
        )
        await pilot.pause()
        footer = str(renderer._footer.render())
        assert footer.startswith("✓ done in ")
        assert "5s" in footer


@pytest.mark.asyncio
async def test_elapsed_seconds_explicit_top_level(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """An explicit top-level ``elapsed_seconds`` wins over ts derivation."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        payload = _mk_payload(WorkflowEventKind.RUN_COMPLETED)
        payload["elapsed_seconds"] = 42
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()
        assert "42s" in str(renderer._footer.render())


def test_parse_ts_accepts_datetime_and_rejects_junk() -> None:
    """``_parse_ts`` returns a datetime as-is and drops non-ISO strings."""
    now = datetime(2026, 7, 27, tzinfo=UTC)
    assert WorkflowRenderer._parse_ts(now) is now
    assert WorkflowRenderer._parse_ts("not-a-timestamp") is None
    assert WorkflowRenderer._parse_ts(12345) is None


def test_elapsed_seconds_no_start_time_is_zero() -> None:
    """With no start_time and no explicit value, elapsed is 0.0 (1346)."""
    renderer = WorkflowRenderer()
    assert renderer._elapsed_seconds({"payload": {}}) == 0.0


def test_elapsed_seconds_nested_duration_seconds() -> None:
    """A nested ``duration_seconds`` is used when no ``elapsed_seconds`` (1341)."""
    renderer = WorkflowRenderer()
    val = renderer._elapsed_seconds({"payload": {"duration_seconds": 7}})
    assert val == 7.0


def test_elapsed_seconds_no_end_ts_is_zero() -> None:
    """With a start_time but no ts and no _last_ts, elapsed is 0.0 (1349)."""
    renderer = WorkflowRenderer()
    renderer._start_time = datetime(2026, 7, 27, tzinfo=UTC)
    renderer._last_ts = None
    assert renderer._elapsed_seconds({"payload": {}}) == 0.0


def test_elapsed_now_without_start_is_zero() -> None:
    """``_elapsed_now`` returns 0.0 before RUN_STARTED lands (1453)."""
    renderer = WorkflowRenderer()
    assert renderer._elapsed_now() == 0.0


def test_elapsed_now_naive_start_time_suppresses_typeerror() -> None:
    """A tz-naive ``_start_time`` makes the subtract raise → suppressed (1457)."""
    renderer = WorkflowRenderer()
    # A naive datetime can't be subtracted from an aware now() → TypeError,
    # which the contextlib.suppress swallows, falling through to 0.0.
    renderer._start_time = datetime(2026, 7, 27, 12, 0, 0)  # noqa: DTZ001 — naive on purpose
    assert renderer._elapsed_now() == 0.0


# ---------------------------------------------------------------------------
# Composite-key lookup fallbacks (1500, 1516-1524, 1540-1542)
# ---------------------------------------------------------------------------


def test_lookup_row_non_string_name_returns_none() -> None:
    """``_lookup_row`` with a non-string node_name returns None (1492)."""
    renderer = WorkflowRenderer()
    assert renderer._lookup_row({"node_name": 5}) is None


def test_lookup_row_no_match_with_invocation_returns_none() -> None:
    """A composite miss with an invocation_id and no placeholder → None (1500)."""
    renderer = WorkflowRenderer()
    assert renderer._lookup_row({"node_name": "x", "invocation_id": "inv_x"}) is None


def test_find_row_by_name_prefers_non_iteration_child() -> None:
    """``_find_row_by_name`` returns the non-child row, else the child (1516-1524)."""
    renderer = WorkflowRenderer()
    child = WorkflowNodeRow(node_name="dup")
    child._is_iteration_child = True
    parent = WorkflowNodeRow(node_name="dup")
    # Insert the child first so the fallback path (1522-1523) is taken
    # before the non-child is found and returned.
    renderer._node_rows[("dup", "inv_child")] = child
    renderer._node_rows[("dup", "inv_parent")] = parent
    assert renderer._find_row_by_name("dup") is parent


def test_find_row_by_name_only_child_returns_child() -> None:
    """When only an iteration child matches, it's returned as fallback."""
    renderer = WorkflowRenderer()
    child = WorkflowNodeRow(node_name="only")
    child._is_iteration_child = True
    renderer._node_rows[("only", "inv_c")] = child
    assert renderer._find_row_by_name("only") is child


def test_find_row_by_name_no_match_returns_none() -> None:
    """No name match → None."""
    renderer = WorkflowRenderer()
    renderer._node_rows[("a", "inv_a")] = WorkflowNodeRow(node_name="a")
    assert renderer._find_row_by_name("nope") is None


def test_find_row_by_invocation_id_miss_returns_none() -> None:
    """``_find_row_by_invocation_id`` returns None on a miss (1542)."""
    renderer = WorkflowRenderer()
    renderer._node_rows[("a", "inv_a")] = WorkflowNodeRow(node_name="a")
    assert renderer._find_row_by_invocation_id("inv_missing") is None
    assert renderer._find_row_by_invocation_id("inv_a") is not None


# ---------------------------------------------------------------------------
# Adapter registration guards (1572, 1608) + apply_snapshot no-op
# ---------------------------------------------------------------------------


def test_register_node_with_adapter_bad_live_map_noop() -> None:
    """A registration where the adapter has no dict live-map is a no-op (1572)."""
    renderer = WorkflowRenderer()

    class _Adapter:
        _live_nodes_by_trace_and_invocation = "not-a-dict"

    renderer.register_adapter(_Adapter())
    row = WorkflowNodeRow(node_name="a")
    # Must not raise even though the map is the wrong type.
    renderer._register_node_with_adapter(row, "tr", "inv")


def test_unregister_node_bad_live_map_noop() -> None:
    """Unregister with a non-dict live-map is a no-op (1608)."""
    renderer = WorkflowRenderer()

    class _Adapter:
        _live_nodes_by_trace_and_invocation = None

    renderer.register_adapter(_Adapter())
    renderer._unregister_node_from_adapter("tr", "inv")


def test_register_node_with_adapter_missing_invocation_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing invocation_id falls back to the sentinel + warns (1575-1585)."""
    import logging

    renderer = WorkflowRenderer()

    class _Adapter:
        def __init__(self) -> None:
            self._live_nodes_by_trace_and_invocation: dict[
                tuple[str, str], WorkflowNodeRow
            ] = {}

    adapter = _Adapter()
    renderer.register_adapter(adapter)
    row = WorkflowNodeRow(node_name="a")
    with caplog.at_level(logging.WARNING):
        renderer._register_node_with_adapter(row, "tr", None)
    assert adapter._live_nodes_by_trace_and_invocation[("tr", "")] is row
    assert any("missing invocation_id" in r.message for r in caplog.records)


def test_apply_snapshot_is_noop() -> None:
    """``apply_snapshot`` accepts content and drops it (no state change)."""
    renderer = WorkflowRenderer()
    renderer.apply_snapshot("anything")
    assert renderer._node_rows == {}
