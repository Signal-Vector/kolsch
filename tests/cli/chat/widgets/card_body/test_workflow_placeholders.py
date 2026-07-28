"""bug-096 T3-T10 - RUN_STARTED pre-mounts placeholder rows.

The workflow renderer reads the topological node manifest from the
NESTED ``payload["payload"]["nodes"]`` (mirroring how it already reads
``inputs``) and pre-mounts one ``WorkflowNodeRow`` per top-level node
in the new ``pending`` state (``○``, dim, non-spinning). When
NODE_STARTED fires, the placeholder is adopted in place (→ ``running``)
via the shared ``_populate_row_from_started`` helper instead of
mounting a new row; NODE_PRUNED transitions it to ``skipped`` (``⊘``).
``_node_rows`` stays STRING-keyed. The header denominator is stable
from RUN_STARTED (``0/N``).

Harness note: ``_mk_payload`` nests the ``nodes`` list under
``payload["payload"]["nodes"]`` — the real wire shape after the bridge
nests the executor domain payload (see "Payload nesting" in the spec's
Technical Context). A test that put ``nodes`` top-level would pass
against a top-level renderer read and both would be wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.event_adapter import EventAdapter
from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from cli.chat.widgets.workflow_node_row import (
    _TERMINAL_STATES,
    WorkflowNodeRow,
)
from workflows.events import WorkflowEventKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


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
    node_type: str | None = None,
    invocation_id: str | None = None,
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Match the WIRE_WORKFLOW_PROGRESS payload shape.

    bug-096: for RUN_STARTED, ``nodes`` is nested under
    ``payload["payload"]["nodes"]`` — the real wire shape after the
    bridge nests the executor domain payload. NODE_STARTED accepts a
    ``node_name`` (and optional ``node_type`` nested under
    ``payload["payload"]["type"]``).
    """
    ts_val = ts or datetime(2026, 7, 5, tzinfo=UTC)
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
    if nodes is not None:
        inner["nodes"] = nodes
    return payload


# ---------------------------------------------------------------------------
# T3 — pre-mount placeholders on RUN_STARTED (nested payload)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_started_premounts_pending_placeholders(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T3: two nested manifest entries → two ``pending`` placeholder rows."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        payload = _mk_payload(
            WorkflowEventKind.RUN_STARTED,
            nodes=[
                {"name": "a", "type": "kolsch/nodes/bash", "predecessors": []},
                {"name": "b", "type": "kolsch/nodes/agent", "predecessors": ["a"]},
            ],
        )
        renderer.apply_event(_MockEvent(_WIRE, payload))
        await pilot.pause()

        # bug-095: placeholders land under the ``(name, "")`` composite
        # key (no invocation_id until NODE_STARTED brings it).
        assert set(renderer._node_rows) == {("a", ""), ("b", "")}
        row_a = renderer._node_rows[("a", "")]
        row_b = renderer._node_rows[("b", "")]
        assert row_a.state == "pending"
        assert row_b.state == "pending"
        # Both carry the `.-pending` CSS class; neither spins
        # (spinner handle only installs for animated states).
        assert row_a.has_class("-pending")
        assert row_b.has_class("-pending")
        assert "○" in row_a.title
        assert "○" in row_b.title
        # Mounted in list order.
        mounted = list(renderer.query(WorkflowNodeRow))
        assert [r._node_name for r in mounted] == ["a", "b"]
        # Header shows 0/2.
        assert "0/2" in str(renderer._header.render())


# ---------------------------------------------------------------------------
# T4 — NODE_STARTED adopts the placeholder in place
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_started_adopts_placeholder_in_place(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T4: NODE_STARTED(a) transitions the placeholder, mounts no new row."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.RUN_STARTED,
                    nodes=[
                        {"name": "a", "type": "kolsch/nodes/bash", "predecessors": []},
                        {
                            "name": "b",
                            "type": "kolsch/nodes/agent",
                            "predecessors": ["a"],
                        },
                    ],
                ),
            )
        )
        await pilot.pause()
        placeholder_a = renderer._node_rows[("a", "")]

        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_STARTED,
                    node_name="a",
                    node_type="kolsch/nodes/bash",
                ),
            )
        )
        await pilot.pause()

        # No new row — still exactly a + b, and the SAME object adopted.
        # bug-095: adopting ``a`` rekeys ``("a", "")`` → ``("a", "inv_a")``;
        # ``b`` is still a pending placeholder under ``("b", "")``.
        assert set(renderer._node_rows) == {("a", "inv_a"), ("b", "")}
        assert renderer._node_rows[("a", "inv_a")] is placeholder_a
        assert placeholder_a.state == "running"
        assert not placeholder_a.has_class("-pending")
        # b untouched.
        assert renderer._node_rows[("b", "")].state == "pending"
        # Header still 0/2 (a is running, not decided).
        assert "0/2" in str(renderer._header.render())


# ---------------------------------------------------------------------------
# T5 — NODE_COMPLETED transitions the adopted row to success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_completed_after_adopt(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T5: NODE_COMPLETED(a) → success; header 1/2."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.RUN_STARTED,
                    nodes=[
                        {"name": "a", "type": "kolsch/nodes/bash", "predecessors": []},
                        {
                            "name": "b",
                            "type": "kolsch/nodes/agent",
                            "predecessors": ["a"],
                        },
                    ],
                ),
            )
        )
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_STARTED,
                    node_name="a",
                    node_type="kolsch/nodes/bash",
                ),
            )
        )
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(WorkflowEventKind.NODE_COMPLETED, node_name="a"),
            )
        )
        await pilot.pause()

        assert renderer._node_rows[("a", "inv_a")].state == "success"
        assert "1/2" in str(renderer._header.render())


# ---------------------------------------------------------------------------
# T6 — NODE_PRUNED transitions a pending placeholder to skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_pruned_transitions_pending_to_skipped(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T6: NODE_PRUNED(a) on a never-started placeholder → skipped."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.RUN_STARTED,
                    nodes=[
                        {"name": "a", "type": "kolsch/nodes/bash", "predecessors": []},
                        {
                            "name": "b",
                            "type": "kolsch/nodes/agent",
                            "predecessors": ["a"],
                        },
                    ],
                ),
            )
        )
        # Pruned events mint a synthetic invocation_id (never a
        # preceding NODE_STARTED); the string-keyed lookup ignores it.
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_PRUNED,
                    node_name="a",
                    invocation_id="inv_synthetic_prune",
                ),
            )
        )
        await pilot.pause()

        # bug-095: the pruned placeholder was never adopted, so it stays
        # under ``("a", "")``; the synthetic prune invocation_id misses
        # the composite key and ``_lookup_row`` falls back to it.
        row_a = renderer._node_rows[("a", "")]
        assert row_a.state == "skipped"
        assert row_a.has_class("-skipped")
        assert "⊘" in row_a.title
        # Header counts skipped as decided → 1/2.
        assert "1/2" in str(renderer._header.render())


# ---------------------------------------------------------------------------
# T7 — old-shape RUN_STARTED without `nodes`
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_shape_run_started_without_nodes(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T7: no manifest → no placeholders; NODE_STARTED fresh-mounts."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        # RUN_STARTED with NO nested `nodes`.
        renderer.apply_event(
            _MockEvent(_WIRE, _mk_payload(WorkflowEventKind.RUN_STARTED))
        )
        await pilot.pause()
        assert renderer._node_rows == {}
        assert renderer._planned_node_count is None

        # NODE_STARTED for a name never seen → fresh-mount in running.
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(WorkflowEventKind.NODE_STARTED, node_name="z"),
            )
        )
        await pilot.pause()
        assert set(renderer._node_rows) == {("z", "inv_z")}
        assert renderer._node_rows[("z", "inv_z")].state == "running"
        # Fallback denominator = len(_node_rows) = 1.
        assert "0/1" in str(renderer._header.render())


# ---------------------------------------------------------------------------
# T8 — adopted agent placeholder registers with the adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adopted_placeholder_registers_with_adapter() -> None:
    """T8: adopt path calls `_register_node_with_adapter` (in the helper).

    Prove that ``_populate_row_from_started`` — which runs on the
    ADOPT branch, not just the fresh-mount branch — registers the row
    in the adapter's ``_live_nodes_by_trace_and_invocation`` map so a
    sub-agent event for the row's ``(trace_id, invocation_id)`` would
    route to it.
    """
    app = _MockChatApp()
    adapter = EventAdapter(app)
    renderer = WorkflowRenderer()
    renderer.register_adapter(adapter)

    renderer.apply_event(
        _MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.RUN_STARTED,
                trace_id="tr_run",
                nodes=[
                    {"name": "b", "type": "kolsch/nodes/agent", "predecessors": []},
                ],
            ),
        )
    )
    # Placeholder present, not yet in the routing map.
    assert renderer._node_rows[("b", "")].state == "pending"
    assert (
        "tr_run",
        "inv_b",
    ) not in adapter._live_nodes_by_trace_and_invocation

    # NODE_STARTED adopts the placeholder AND registers it.
    renderer.apply_event(
        _MockEvent(
            _WIRE,
            _mk_payload(
                WorkflowEventKind.NODE_STARTED,
                node_name="b",
                node_type="kolsch/nodes/agent",
                trace_id="tr_run",
                invocation_id="inv_b",
            ),
        )
    )
    # bug-095: adopting ``b`` rekeys ``("b", "")`` → ``("b", "inv_b")``.
    assert renderer._node_rows[("b", "inv_b")].state == "running"
    routed = adapter._live_nodes_by_trace_and_invocation.get(("tr_run", "inv_b"))
    assert routed is renderer._node_rows[("b", "inv_b")]


# ---------------------------------------------------------------------------
# T9 — placeholder for a nested workflow node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_placeholder_for_nested_workflow_node(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T9: a `kolsch/nodes/workflow` node is ONE placeholder that adopts."""
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.RUN_STARTED,
                    nodes=[
                        {
                            "name": "sub",
                            "type": "kolsch/nodes/workflow",
                            "predecessors": [],
                        },
                    ],
                ),
            )
        )
        await pilot.pause()
        # Exactly one placeholder — no cross-card pre-mount of children.
        assert set(renderer._node_rows) == {("sub", "")}
        assert renderer._node_rows[("sub", "")]._node_type == "kolsch/nodes/workflow"

        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_payload(
                    WorkflowEventKind.NODE_STARTED,
                    node_name="sub",
                    node_type="kolsch/nodes/workflow",
                ),
            )
        )
        await pilot.pause()
        # bug-095: adopting rekeys ``("sub", "")`` → ``("sub", "inv_sub")``.
        assert set(renderer._node_rows) == {("sub", "inv_sub")}
        assert renderer._node_rows[("sub", "inv_sub")].state == "running"
        assert (
            renderer._node_rows[("sub", "inv_sub")]._node_type
            == "kolsch/nodes/workflow"
        )


# ---------------------------------------------------------------------------
# T10 — pending is non-terminal (no buffer-freeze); skipped freezes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_is_non_terminal_skipped_is_terminal(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """T10: pending does not freeze the buffer; skipped does."""
    pending_row = WorkflowNodeRow(node_name="p", initial_state="pending")
    app = mock_app_factory(pending_row)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert pending_row.state == "pending"
        assert pending_row.has_class("-pending")
        # `pending` is NOT in the buffer-freeze set.
        assert "pending" not in _TERMINAL_STATES
        assert pending_row._buffer_frozen is False

        # A subsequent transition to running removes `.-pending`.
        pending_row.set_state("running")
        await pilot.pause()
        assert not pending_row.has_class("-pending")
        assert pending_row._buffer_frozen is False

    # A skipped row freezes its buffer.
    skipped_row = WorkflowNodeRow(node_name="s", initial_state="skipped")
    app2 = mock_app_factory(skipped_row)
    async with app2.run_test() as pilot2:
        await pilot2.pause()
        assert skipped_row.state == "skipped"
        assert "skipped" in _TERMINAL_STATES
        assert skipped_row._buffer_frozen is True


# ---------------------------------------------------------------------------
# _MockChatApp — minimal sink for the EventAdapter T8 path.
# ---------------------------------------------------------------------------


class _MockChatApp:
    """Minimal ChatApp stand-in for EventAdapter construction (T8).

    Mirrors the shape used by ``test_workflow_trace_routing.py`` — the
    adapter only needs the sink method surface + last-assistant text
    accessors to construct and register a live node.
    """

    def __init__(self) -> None:
        self.mounts: list[str] = []
        self._last_assistant: str = ""

    async def mount_user_message(
        self,
        text: str,
        timestamp: Any = None,
        *,
        event_id: Any = None,
        content: Any = None,
    ) -> None:
        _ = timestamp, event_id, content
        self.mounts.append(f"user:{text}")

    async def mount_assistant_message(
        self,
        text: str,
        *,
        event_id: Any = None,
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
