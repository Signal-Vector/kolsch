"""bug-095 T1-T9 — map iterations render as child rows under the map header.

A ``kolsch/nodes/map`` node spawns N iterations of one applied node;
every iteration's ``NODE_STARTED`` carries the SAME ``node_name`` (the
applied node), so the pre-bug-095 ``node_name``-keyed ``_node_rows`` map
deduped all but the first away. bug-095 rekeys ``_node_rows`` on the
composite ``(node_name, invocation_id)`` and mounts each iteration as a
CHILD row inside the parent map row's ``Collapsible.Contents``, ordered
by ``iteration_index``, collapsed by default when N>10, with a live
rollup counter on the map header title.

Wire-shape note (load-bearing): the child's own ``invocation_id`` is
hoisted TOP-LEVEL by the bridge (``payload["invocation_id"]``), while the
iteration correlation fields (``parent_invocation_id``,
``iteration_index``, ``iteration_total``) stay NESTED under
``payload["payload"]`` — the same split the existing summary / output
handlers already use. ``_mk_payload`` below builds both halves.

Harness mirrors ``test_workflow_renderer.py`` (``_MockEvent`` +
``_mk_payload`` + ``mock_app_factory``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from textual.widgets import Collapsible

from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.wire_kinds import WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT
from workflows.events import WorkflowEventKind

_WIRE = "kolsch.workflow.progress"
_MAP_TYPE = "kolsch/nodes/map"


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
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
    error_message: str | None = None,
    summary: str | None = None,
    # bug-095 iteration correlation — NESTED under payload["payload"].
    parent_invocation_id: str | None = None,
    iteration_index: int | None = None,
    iteration_total: int | None = None,
) -> dict[str, Any]:
    """Build a WIRE_WORKFLOW_PROGRESS payload with optional iteration fields.

    The child's own ``invocation_id`` is hoisted TOP-LEVEL (the bridge
    does this); ``parent_invocation_id`` / ``iteration_index`` /
    ``iteration_total`` stay NESTED under ``payload["payload"]``.
    """
    ts_val = ts or datetime(2026, 7, 26, tzinfo=UTC)
    inner: dict[str, Any] = {}
    payload: dict[str, Any] = {
        "run_id": run_id,
        "workflow_ref": "kolsch/workflows/test",
        "kind": workflow_event_kind.value,
        "event_id": "ev_test",
        "ts": ts_val.isoformat(),
        "trace_id": trace_id,
        "payload": inner,
    }
    if node_name is not None:
        payload["node_name"] = node_name
        payload["invocation_id"] = invocation_id or f"inv_{node_name}"
    if node_type is not None:
        inner["type"] = node_type
    if error_message is not None:
        inner["error_message"] = error_message
    if summary is not None:
        inner["summary"] = summary
    if parent_invocation_id is not None:
        inner["parent_invocation_id"] = parent_invocation_id
    if iteration_index is not None:
        inner["iteration_index"] = iteration_index
    if iteration_total is not None:
        inner["iteration_total"] = iteration_total
    return payload


def _mk_sub_agent_payload(
    *,
    trace_id: str,
    invocation_id: str,
    node_name: str,
    run_id: str = "wrk_test",
    event_type: str = "config.set",
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


async def _seed_map(
    renderer: WorkflowRenderer,
    pilot: Any,
    *,
    map_invocation: str = "inv_p",
) -> None:
    """RUN_STARTED + map NODE_STARTED so a parent map row exists."""
    renderer.apply_event(_MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED)))
    renderer.apply_event(
        _MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.NODE_STARTED,
                node_name="score_resumes",
                invocation_id=map_invocation,
                node_type=_MAP_TYPE,
            ),
        )
    )
    await pilot.pause()


def _iter_started(
    *,
    index: int,
    total: int,
    parent_invocation: str = "inv_p",
    child_name: str = "score_one_resume",
) -> _MockEvent:
    """A single iteration's NODE_STARTED event."""
    return _MockEvent(
        _WIRE,
        _mk_payload(
            WorkflowEventKind.NODE_STARTED,
            node_name=child_name,
            invocation_id=f"inv_child_{index}",
            parent_invocation_id=parent_invocation,
            iteration_index=index,
            iteration_total=total,
        ),
    )


def _map_row(renderer: WorkflowRenderer) -> WorkflowNodeRow:
    return renderer._node_rows[("score_resumes", "inv_p")]


# ---------------------------------------------------------------------------
# T1 — 30 iteration children mount under the map row (collapsed, N>10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thirty_iterations_mount_as_children(mock_app_factory) -> None:
    """T1: 30 iterations → 31 rows total; all children collapsed."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_map(renderer, pilot)
        for i in range(30):
            renderer.apply_event(_iter_started(index=i, total=30))
        await pilot.pause()

        # map header + 30 children.
        assert len(renderer._node_rows) == 31
        parent = _map_row(renderer)
        assert set(parent._iteration_children.keys()) == set(range(30))

        # Children live inside the parent's Collapsible.Contents, NOT in
        # the top-level node list.
        contents = parent.query_one(Collapsible.Contents)
        child_rows = list(contents.query(WorkflowNodeRow))
        assert len(child_rows) == 30
        # No iteration child is a DIRECT child of the node list (they nest
        # inside the map row's Contents). ``_node_list.query`` walks
        # recursively, so filter to direct children.
        direct = [
            w for w in renderer._node_list.children if isinstance(w, WorkflowNodeRow)
        ]
        assert [r for r in direct if r._is_iteration_child] == []
        assert parent in direct

        # Every child is collapsed by default (30 > 10).
        assert all(c.collapsed for c in parent._iteration_children.values())
        # Each child stamped as an iteration child.
        assert all(c._is_iteration_child for c in parent._iteration_children.values())


# ---------------------------------------------------------------------------
# T2 — small map (N=5) leaves children expanded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_small_map_children_expanded(mock_app_factory) -> None:
    """T2: 5 iterations (≤10) → children NOT collapsed by default."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_map(renderer, pilot)
        for i in range(5):
            renderer.apply_event(_iter_started(index=i, total=5))
        await pilot.pause()

        parent = _map_row(renderer)
        assert len(parent._iteration_children) == 5
        assert all(not c.collapsed for c in parent._iteration_children.values())


# ---------------------------------------------------------------------------
# T3 — iterations mount in index order regardless of arrival order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iterations_mount_in_index_order(mock_app_factory) -> None:
    """T3: shuffled arrival → DOM order is index-ascending."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_map(renderer, pilot)
        for i in [3, 0, 5, 1, 2, 4]:
            renderer.apply_event(_iter_started(index=i, total=6))
        await pilot.pause()

        parent = _map_row(renderer)
        contents = parent.query_one(Collapsible.Contents)
        dom_order = [c._iteration_index for c in contents.query(WorkflowNodeRow)]
        assert dom_order == [0, 1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# T4 — idempotent replay does not double-mount OR reset state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_does_not_double_mount_or_reset(mock_app_factory) -> None:
    """T4: replayed NODE_STARTED for a completed iteration is a no-op."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_map(renderer, pilot)
        # Iteration 7 starts, then completes.
        renderer.apply_event(_iter_started(index=7, total=30))
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_COMPLETED,
                    node_name="score_one_resume",
                    invocation_id="inv_child_7",
                    parent_invocation_id="inv_p",
                    iteration_index=7,
                ),
            )
        )
        await pilot.pause()
        parent = _map_row(renderer)
        assert parent._iteration_children[7].state == "success"

        # Replay the SAME iteration's NODE_STARTED (same invocation_id).
        renderer.apply_event(_iter_started(index=7, total=30))
        await pilot.pause()

        # Still exactly ONE child at index 7, state NOT reset to running.
        assert list(parent._iteration_children.keys()) == [7]
        assert parent._iteration_children[7].state == "success"
        # Composite key still holds one entry for that child.
        assert ("score_one_resume", "inv_child_7") in renderer._node_rows


# ---------------------------------------------------------------------------
# T5 — COMPLETED / FAILED events reach the correct child + rollup updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_failed_reach_correct_child(mock_app_factory) -> None:
    """T5: iteration 7 succeeds, 15 fails; header rollup reflects both."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_map(renderer, pilot)
        for i in range(30):
            renderer.apply_event(_iter_started(index=i, total=30))
        await pilot.pause()

        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_COMPLETED,
                    node_name="score_one_resume",
                    invocation_id="inv_child_7",
                    parent_invocation_id="inv_p",
                    iteration_index=7,
                ),
            )
        )
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_FAILED,
                    node_name="score_one_resume",
                    invocation_id="inv_child_15",
                    parent_invocation_id="inv_p",
                    iteration_index=15,
                    error_message="boom",
                ),
            )
        )
        await pilot.pause()

        parent = _map_row(renderer)
        assert parent._iteration_children[7].state == "success"
        assert parent._iteration_children[15].state == "failed"
        others = [
            c for idx, c in parent._iteration_children.items() if idx not in (7, 15)
        ]
        assert all(c.state == "running" for c in others)
        # Rollup on the map header title.
        assert "(map · 30 iterations · 1 done, 28 running, 1 failed)" in parent.title


# ---------------------------------------------------------------------------
# T6 — sub-agent events reach the correct child row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_event_reaches_correct_child(mock_app_factory) -> None:
    """T6: a sub-agent event for child[3]'s invocation lands on child[3]."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_map(renderer, pilot)
        for i in range(30):
            renderer.apply_event(_iter_started(index=i, total=30))
        await pilot.pause()

        # Adapter-less fallback: a lifecycle sub-agent event bumps the
        # routed row's counter. Route on child[3]'s invocation_id.
        renderer.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
                _mk_sub_agent_payload(
                    trace_id="tr_test",
                    invocation_id="inv_child_3",
                    node_name="score_one_resume",
                    event_type="config.set",
                ),
            )
        )
        await pilot.pause()
        await pilot.pause()

        parent = _map_row(renderer)
        assert parent._iteration_children[3]._sub_agent_turn_count == 1
        others = [c for idx, c in parent._iteration_children.items() if idx != 3]
        assert all(c._sub_agent_turn_count == 0 for c in others)


# ---------------------------------------------------------------------------
# T7 — header counter excludes iteration children
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_header_counter_excludes_iteration_children(mock_app_factory) -> None:
    """T7: a 30-iteration map + 2 nodes reads 3/3, not 33/33."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _seed_map(renderer, pilot)
        # 30 iterations start then complete.
        for i in range(30):
            renderer.apply_event(_iter_started(index=i, total=30))
        for i in range(30):
            renderer.apply_event(
                _MockEvent(
                    _WIRE,
                    _mk_payload(
                        WorkflowEventKind.NODE_COMPLETED,
                        node_name="score_one_resume",
                        invocation_id=f"inv_child_{i}",
                        parent_invocation_id="inv_p",
                        iteration_index=i,
                    ),
                )
            )
        # The map node itself completes.
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_COMPLETED,
                    node_name="score_resumes",
                    invocation_id="inv_p",
                ),
            )
        )
        # Two other top-level nodes start + complete.
        for name in ("prep", "report"):
            renderer.apply_event(
                _MockEvent(
                    _WIRE,
                    _mk_payload(WorkflowEventKind.NODE_STARTED, node_name=name),
                )
            )
            renderer.apply_event(
                _MockEvent(
                    _WIRE,
                    _mk_payload(WorkflowEventKind.NODE_COMPLETED, node_name=name),
                )
            )
        await pilot.pause()

        header = str(renderer._header.render())
        assert "3/3" in header
        assert "33/33" not in header

        # Compact mode inherits the same counts via the header.
        renderer.compact_mode = True
        await pilot.pause()
        compact = str(renderer._compact_line.render())
        assert "3/3" in compact


# ---------------------------------------------------------------------------
# T8 — a non-iteration node still mounts at the workflow level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_iteration_node_mounts_at_workflow_level(mock_app_factory) -> None:
    """T8: a bash node with no parent_invocation_id mounts in the node list."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
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
                    node_name="run_bash",
                    node_type="kolsch/nodes/bash",
                ),
            )
        )
        await pilot.pause()

        row = renderer._node_rows[("run_bash", "inv_run_bash")]
        assert not row._is_iteration_child
        top_level = list(renderer._node_list.query(WorkflowNodeRow))
        assert row in top_level


# ---------------------------------------------------------------------------
# T9 — old-shape payload without invocation_id falls back to (name, "")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_shape_payload_falls_back(mock_app_factory) -> None:
    """T9: no invocation_id → (name, "") key; a second same-name STARTED dedups."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        # NODE_STARTED with NO top-level invocation_id.
        payload = _mk_payload(WorkflowEventKind.NODE_STARTED, node_name="legacy")
        del payload["invocation_id"]
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()

        assert ("legacy", "") in renderer._node_rows
        first_row = renderer._node_rows[("legacy", "")]

        # A second same-name STARTED (also missing invocation_id) hits the
        # dedup guard on ("legacy", "") — no second row.
        payload2 = _mk_payload(WorkflowEventKind.NODE_STARTED, node_name="legacy")
        del payload2["invocation_id"]
        renderer.apply_event(_MockEvent(_WIRE, payload2))
        await pilot.pause()

        assert renderer._node_rows[("legacy", "")] is first_row
        assert sum(1 for k in renderer._node_rows if k[0] == "legacy") == 1
