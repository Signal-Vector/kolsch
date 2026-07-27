"""spec-103 T7-T9 — child sub-workflow renders as its own peer card.

A `kolsch/nodes/workflow` child surfaces as its OWN flat peer
`WorkflowRenderer` card (never nested inside the parent's node row),
with a `↳ from <parent-ref>` breadcrumb, its own node list, and full
agent-node sub-agent drill-in.

These are widget/e2e tests: the parent + child cards are TWO
independent `WorkflowRenderer` widgets (the flat-peers invariant), each
fed its own `kolsch.workflow.progress` stream tagged with its own
run_id. The breadcrumb is driven through the adapter's run_id-keyed
`_refresh_workflow_parent_ref` path (the same path the service's child
record's `parent_run_id` fires in production).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from textual.app import App, ComposeResult

from cli.chat.event_adapter import EventAdapter
from cli.chat.widgets.card_body.workflow import (
    WorkflowRenderer,
    _ReconstructedSubAgentEvent,
)
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.events import EventType
from schemas.wire_kinds import (
    WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT,
    WIRE_WORKFLOW_PROGRESS,
)
from workflows.events import WorkflowEventKind

if TYPE_CHECKING:
    from textual.widget import Widget


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


def _mk_payload(
    workflow_event_kind: WorkflowEventKind,
    *,
    run_id: str,
    node_name: str | None = None,
    trace_id: str = "tr_shared",
    ts: datetime | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ts_val = ts or datetime(2026, 7, 27, tzinfo=UTC)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "workflow_ref": "b/workflow/x",
        "kind": workflow_event_kind.value,
        "event_id": "ev_test",
        "ts": ts_val.isoformat(),
        "trace_id": trace_id,
        "payload": dict(extra_payload or {}),
    }
    if node_name is not None:
        payload["node_name"] = node_name
        payload["invocation_id"] = f"inv_{node_name}"
    return payload


def _mk_sub_agent_payload(
    *,
    run_id: str,
    trace_id: str,
    invocation_id: str,
    node_name: str,
    event_type: str = "agent.message",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "node_name": node_name,
        "invocation_id": invocation_id,
        "trace_id": trace_id,
        "event_type": event_type,
        "event_id": "ev_sa",
        "payload": dict(payload or {}),
    }


class _StubRecord:
    def __init__(self, *, ref: str, parent_run_id: str | None = None) -> None:
        self.ref = ref
        self.parent_run_id = parent_run_id
        self.card = None


class _StubWorkflowsImpl:
    def __init__(self, registry: dict[str, _StubRecord]) -> None:
        self._workflow_registry = registry


class _NoTranscriptApp:
    def query_one(self, *_a: object, **_kw: object) -> object:
        raise LookupError("no #transcript")


def _multi_renderer_app_factory(*renderers: Widget) -> App:
    """Host app that mounts several `WorkflowRenderer`s as flat peers."""

    class _HostApp(App):
        def compose(self) -> ComposeResult:
            yield from renderers

    return _HostApp()


# ---------------------------------------------------------------------------
# T7 — child renders as a peer card with a `↳ from <parent>` breadcrumb.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_renders_as_peer_with_breadcrumb() -> None:
    parent_run = f"wrk_{uuid4()}"
    child_run = f"wrk_{uuid4()}"

    parent = WorkflowRenderer()
    child = WorkflowRenderer()
    app = _multi_renderer_app_factory(parent, child)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Parent stream: RUN_STARTED → NODE_STARTED(workflow node) →
        # CALLED_WORKFLOW.
        parent.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_PROGRESS,
                _mk_payload(WorkflowEventKind.RUN_STARTED, run_id=parent_run),
            )
        )
        parent.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_PROGRESS,
                _mk_payload(
                    WorkflowEventKind.NODE_STARTED,
                    run_id=parent_run,
                    node_name="sub",
                ),
            )
        )
        parent.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_PROGRESS,
                _mk_payload(
                    WorkflowEventKind.CALLED_WORKFLOW,
                    run_id=parent_run,
                    node_name="sub",
                    extra_payload={
                        "node_name": "sub",
                        "invocation_id": "inv_sub",
                        "child_workflow_run_id": child_run,
                        "child_workflow_ref": "b/workflow/child",
                    },
                ),
            )
        )
        await pilot.pause()

        # Child stream (its OWN card): RUN_STARTED + two node events.
        child.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_PROGRESS,
                _mk_payload(WorkflowEventKind.RUN_STARTED, run_id=child_run),
            )
        )
        for node in ("child_a", "child_b"):
            child.apply_event(
                _MockEvent(
                    WIRE_WORKFLOW_PROGRESS,
                    _mk_payload(
                        WorkflowEventKind.NODE_STARTED,
                        run_id=child_run,
                        node_name=node,
                    ),
                )
            )
        await pilot.pause()

        # Two distinct WorkflowRenderer cards exist as peers — the child
        # is NOT mounted inside the parent's node row.
        assert parent is not child
        assert parent._run_id == parent_run
        assert child._run_id == child_run
        parent_rows = {r._node_name for r in parent.query(WorkflowNodeRow)}
        child_rows = {r._node_name for r in child.query(WorkflowNodeRow)}
        assert "sub" in parent_rows
        # The child's node list reflects the CHILD's nodes, on the CHILD.
        assert {"child_a", "child_b"} <= child_rows
        # And the child's nodes are NOT mounted inside the parent card.
        assert "child_a" not in parent_rows
        assert "child_b" not in parent_rows

        # Breadcrumb: the run_id-keyed adapter path sets `↳ from <parent>`
        # from the child record's `parent_run_id` (production wiring).
        registry = {
            parent_run: _StubRecord(ref="b/workflow/parent"),
            child_run: _StubRecord(
                ref="b/workflow/child",
                parent_run_id=parent_run,
            ),
        }
        adapter = EventAdapter(
            _NoTranscriptApp(),  # type: ignore[arg-type]
            workflows_impl=_StubWorkflowsImpl(registry),  # type: ignore[arg-type]
        )
        adapter._refresh_workflow_parent_ref(child, child_run)
        await pilot.pause()

        assert child._parent_ref == "b/workflow/parent"
        assert "↳ from b/workflow/parent" in child._header._compute_text()
        # The parent card has NO breadcrumb (it is top-level).
        assert parent._parent_ref is None


# ---------------------------------------------------------------------------
# T8 — child agent node drill-in shows sub-agent detail.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_agent_node_drill_in_shows_sub_agent_detail(
    mock_app_factory: Any,
) -> None:
    child_run = f"wrk_{uuid4()}"

    child = WorkflowRenderer()
    app = mock_app_factory(child)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Child's agent node started.
        child.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_PROGRESS,
                _mk_payload(WorkflowEventKind.RUN_STARTED, run_id=child_run),
            )
        )
        child.apply_event(
            _MockEvent(
                WIRE_WORKFLOW_PROGRESS,
                _mk_payload(
                    WorkflowEventKind.NODE_STARTED,
                    run_id=child_run,
                    node_name="classify",
                ),
            )
        )
        await pilot.pause()

        row = child._node_rows[("classify", "inv_classify")]
        row.collapsed = False
        await pilot.pause()

        # No adapter wired → local fallback path: the row's sink method
        # receives the sub-agent assistant message (drill-in detail).
        received: list[str] = []

        async def _spy(text: str, *args: Any, **kwargs: Any) -> None:
            _ = args, kwargs
            received.append(text)

        row.mount_assistant_message = _spy  # type: ignore[method-assign]

        # The child agent node's sub-agent event. The renderer routes it
        # to the row by `node_name` (invocation is unmatched in the
        # adapter map, so the local fallback fires).
        sa_payload = _mk_sub_agent_payload(
            run_id=child_run,
            trace_id="tr_shared",
            invocation_id="inv_classify",
            node_name="classify",
            event_type="agent.message",
            payload={"content": {"text": "child-agent-said-hi"}},
        )
        child.apply_event(_MockEvent(WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT, sa_payload))
        await pilot.pause()
        await pilot.pause()

    assert received == ["child-agent-said-hi"]


@pytest.mark.asyncio
async def test_child_agent_node_drill_in_via_adapter(
    mock_app_factory: Any,
) -> None:
    """Adapter path for the child agent node — the reconstructed event
    reaches the adapter's dispatch entrypoint (same as a top-level
    agent node), keyed on the child's `(trace_id, invocation_id)`."""
    child_run = f"wrk_{uuid4()}"

    child = WorkflowRenderer()
    app = mock_app_factory(child)
    async with app.run_test() as pilot:
        await pilot.pause()

        dispatched: list[tuple[Any, str | None]] = []

        class _StubAdapter:
            def __init__(self) -> None:
                self._live_nodes_by_trace_and_invocation: dict[
                    tuple[str, str], WorkflowNodeRow
                ] = {}

            async def dispatch_sub_agent_event(
                self,
                inner_event: Any,
                *,
                outer_trace_id: str | None = None,
            ) -> None:
                dispatched.append((inner_event, outer_trace_id))

        adapter = _StubAdapter()
        child.register_adapter(adapter)

        row = WorkflowNodeRow(node_name="classify", trace_id="tr_shared")
        # bug-095: _node_rows is composite-keyed (name, invocation_id).
        child._node_rows[("classify", "inv_c")] = row
        adapter._live_nodes_by_trace_and_invocation[("tr_shared", "inv_c")] = row

        sa_payload = _mk_sub_agent_payload(
            run_id=child_run,
            trace_id="tr_shared",
            invocation_id="inv_c",
            node_name="classify",
            event_type="tool.called",
            payload={"tool": "search", "args": {"q": "x"}},
        )
        child.apply_event(_MockEvent(WIRE_WORKFLOW_NODE_SUB_AGENT_EVENT, sa_payload))
        await pilot.pause()

    assert len(dispatched) == 1
    evt, outer_trace = dispatched[0]
    assert isinstance(evt, _ReconstructedSubAgentEvent)
    assert evt.type == EventType.TOOL_CALLED
    assert outer_trace == "tr_shared"


# ---------------------------------------------------------------------------
# T9 — nested grandchild gets its own peer card.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grandchild_gets_its_own_peer_card() -> None:
    parent_run = f"wrk_{uuid4()}"
    child_run = f"wrk_{uuid4()}"
    grandchild_run = f"wrk_{uuid4()}"

    parent = WorkflowRenderer()
    child = WorkflowRenderer()
    grandchild = WorkflowRenderer()
    app = _multi_renderer_app_factory(parent, child, grandchild)

    async with app.run_test() as pilot:
        await pilot.pause()

        # Each level anchors its own card via RUN_STARTED + a node.
        for renderer, rid, node in (
            (parent, parent_run, "p_node"),
            (child, child_run, "c_node"),
            (grandchild, grandchild_run, "g_node"),
        ):
            renderer.apply_event(
                _MockEvent(
                    WIRE_WORKFLOW_PROGRESS,
                    _mk_payload(WorkflowEventKind.RUN_STARTED, run_id=rid),
                )
            )
            renderer.apply_event(
                _MockEvent(
                    WIRE_WORKFLOW_PROGRESS,
                    _mk_payload(
                        WorkflowEventKind.NODE_STARTED,
                        run_id=rid,
                        node_name=node,
                    ),
                )
            )
        await pilot.pause()

        # Three peer cards, each anchored to its own run id.
        anchors = {parent._run_id, child._run_id, grandchild._run_id}
        assert anchors == {parent_run, child_run, grandchild_run}

        # Each deeper card shows `↳ from` its IMMEDIATE parent — no
        # indentation growth (they're flat peers; the breadcrumb is the
        # only lineage cue).
        registry = {
            parent_run: _StubRecord(ref="b/workflow/parent"),
            child_run: _StubRecord(ref="b/workflow/child", parent_run_id=parent_run),
            grandchild_run: _StubRecord(
                ref="b/workflow/grandchild", parent_run_id=child_run
            ),
        }
        adapter = EventAdapter(
            _NoTranscriptApp(),  # type: ignore[arg-type]
            workflows_impl=_StubWorkflowsImpl(registry),  # type: ignore[arg-type]
        )
        adapter._refresh_workflow_parent_ref(child, child_run)
        adapter._refresh_workflow_parent_ref(grandchild, grandchild_run)
        await pilot.pause()

        assert parent._parent_ref is None
        assert child._parent_ref == "b/workflow/parent"
        assert grandchild._parent_ref == "b/workflow/child"
