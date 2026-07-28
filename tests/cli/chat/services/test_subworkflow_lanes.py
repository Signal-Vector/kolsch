"""spec-103 T3-T6 — WorkflowsImpl child-lane lifecycle (service seam).

Exercises the run-dispatching event observer, the synchronous
child-bridge race guard, the `_open_child_lane` card/record/drain
lifecycle, child-event routing to the child card's envelope, and the
child lane's clean shutdown on `CALLED_WORKFLOW_COMPLETED`.

These are service-level unit/integration tests: they drive the
observer + drain machinery directly (feeding typed `WorkflowEvent`s)
rather than standing up a real nested executor — the engine-side
observer threading is covered by
`tests/component/workflows/test_subworkflow_observer_propagation.py`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.workflows_impl import (
    _STOP_SENTINEL,
    WorkflowRunRecord,
    WorkflowsImpl,
)
from schemas.events import EventType
from schemas.wire_kinds import WIRE_WORKFLOW_PROGRESS
from store import SessionStore, SqliteBackend
from workflows.events import (
    WorkflowNodeCalledWorkflowCompletedEvent,
    WorkflowNodeCalledWorkflowCompletedPayload,
    WorkflowNodeCalledWorkflowEvent,
    WorkflowNodeCalledWorkflowPayload,
    WorkflowNodeStartedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    platform_dir = tmp_path / ".platform"
    platform_dir.mkdir(exist_ok=True)
    backend = SqliteBackend(platform_dir / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


async def _mk(
    tmp_path: Path,
    store: SessionStore,
) -> tuple[WorkflowsImpl, CardsImpl]:
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    cards = CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    impl = WorkflowsImpl(
        runtime=None,
        cards=cards,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )
    return impl, cards


def _record(run_id: WorkflowRunId, ref: str = "b/workflow/x") -> WorkflowRunRecord:
    from datetime import UTC, datetime

    return WorkflowRunRecord(
        run_id=run_id,
        ref=ref,
        phase="running",
        card=None,
        created_at=datetime.now(tz=UTC),
    )


def _called_workflow_event(
    *,
    parent_run_id: WorkflowRunId,
    trace_id: TraceId,
    child_run_id: WorkflowRunId,
    child_ref: str,
    node_name: str = "sub",
) -> WorkflowNodeCalledWorkflowEvent:
    inv = InvocationId(f"inv_{uuid4()}")
    return WorkflowNodeCalledWorkflowEvent(
        workflow_run_id=parent_run_id,
        trace_id=trace_id,
        invocation_id=inv,
        node_name=node_name,
        payload=WorkflowNodeCalledWorkflowPayload(
            node_name=node_name,
            invocation_id=str(inv),
            child_workflow_run_id=str(child_run_id),
            child_workflow_ref=child_ref,
        ),
    )


def _called_completed_event(
    *,
    parent_run_id: WorkflowRunId,
    trace_id: TraceId,
    child_run_id: WorkflowRunId,
    child_status: str = "succeeded",
    node_name: str = "sub",
) -> WorkflowNodeCalledWorkflowCompletedEvent:
    inv = InvocationId(f"inv_{uuid4()}")
    return WorkflowNodeCalledWorkflowCompletedEvent(
        workflow_run_id=parent_run_id,
        trace_id=trace_id,
        invocation_id=inv,
        node_name=node_name,
        payload=WorkflowNodeCalledWorkflowCompletedPayload(
            node_name=node_name,
            invocation_id=str(inv),
            child_workflow_run_id=str(child_run_id),
            child_status=child_status,
        ),
    )


async def _collect_progress(
    store: SessionStore, thread_id: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == WIRE_WORKFLOW_PROGRESS:
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                out.append(payload)
    return out


# ---------------------------------------------------------------------------
# T3 — run-dispatching observer routes by run_id.
# ---------------------------------------------------------------------------


async def test_dispatch_observer_routes_by_run_id(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk(tmp_path, store)
    try:
        run_a = WorkflowRunId(f"wrk_{uuid4()}")
        run_b = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")

        q_a: asyncio.Queue[Any] = asyncio.Queue()
        q_b: asyncio.Queue[Any] = asyncio.Queue()
        impl._workflow_bridges[run_a] = q_a
        impl._workflow_bridges[run_b] = q_b
        impl._workflow_registry[run_a] = _record(run_a)
        impl._workflow_registry[run_b] = _record(run_b)

        # Home is run_a; a run_b event must still land in run_b's queue
        # (run_b IS a registered lane, so it does not hit the home
        # fallback).
        observer = impl._make_dispatch_event_observer(
            run_a, impl._workflow_registry[run_a]
        )

        ev_b = WorkflowRunStartedEvent(workflow_run_id=run_b, trace_id=tr)
        observer(ev_b)

        assert q_b.qsize() == 1
        assert q_a.qsize() == 0
        assert q_b.get_nowait() is ev_b

        # An event for a run with no lane AND not the home run: it falls
        # back to the home bridge (queue-id vs store-id reconciliation)
        # rather than raising.
        run_c = WorkflowRunId(f"wrk_{uuid4()}")
        ev_c = WorkflowRunStartedEvent(workflow_run_id=run_c, trace_id=tr)
        observer(ev_c)
        assert q_a.qsize() == 1  # home fallback
        assert q_a.get_nowait() is ev_c
    finally:
        await impl.aclose()
        await cards.aclose()


# ---------------------------------------------------------------------------
# T4 — CALLED_WORKFLOW opens a child lane (card + record + drain),
#      idempotent; child bridge created synchronously (race guard).
# ---------------------------------------------------------------------------


async def test_called_workflow_opens_child_lane(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk(tmp_path, store)
    try:
        parent = WorkflowRunId(f"wrk_{uuid4()}")
        child = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        impl._workflow_registry[parent] = _record(parent, ref="b/workflow/parent")
        impl._workflow_bridges[parent] = asyncio.Queue()

        observer = impl._make_dispatch_event_observer(
            parent, impl._workflow_registry[parent]
        )

        called = _called_workflow_event(
            parent_run_id=parent,
            trace_id=tr,
            child_run_id=child,
            child_ref="b/workflow/child",
        )
        # The observer sees CALLED_WORKFLOW → child bridge created
        # SYNCHRONOUSLY (race guard) BEFORE any async lane setup.
        observer(called)
        assert child in impl._workflow_bridges, (
            "child bridge must be created synchronously on CALLED_WORKFLOW"
        )

        # Now the async lane setup (as the parent drain would call it).
        await impl._open_child_lane(parent, called.payload)

        # Card opened once.
        rec = impl._workflow_registry.get(child)
        assert rec is not None
        assert rec.parent_run_id == parent
        assert rec.ref == "b/workflow/child"
        assert rec.card is not None
        assert child in impl._children_of[parent]
        assert child in impl._per_run_drain_tasks
        assert not impl._per_run_drain_tasks[child].done()

        card_id_first = rec.card.id

        # Idempotent — replaying the same CALLED_WORKFLOW does NOT open a
        # second card / record / drain.
        task_first = impl._per_run_drain_tasks[child]
        await impl._open_child_lane(parent, called.payload)
        assert impl._workflow_registry[child].card.id == card_id_first
        assert impl._per_run_drain_tasks[child] is task_first
        assert impl._children_of[parent].count(child) == 1
    finally:
        await impl.aclose()
        await cards.aclose()


# ---------------------------------------------------------------------------
# T5 — child events route to the child card's UI envelope.
# ---------------------------------------------------------------------------


async def test_child_events_route_to_child_envelope(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk(tmp_path, store)
    try:
        parent = WorkflowRunId(f"wrk_{uuid4()}")
        child = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        impl._workflow_registry[parent] = _record(parent, ref="b/workflow/parent")
        impl._workflow_bridges[parent] = asyncio.Queue()

        observer = impl._make_dispatch_event_observer(
            parent, impl._workflow_registry[parent]
        )
        called = _called_workflow_event(
            parent_run_id=parent,
            trace_id=tr,
            child_run_id=child,
            child_ref="b/workflow/child",
        )
        observer(called)
        await impl._open_child_lane(parent, called.payload)

        # Feed the child's own events through the dispatch observer. Each
        # carries the child's run_id, so they route to the child bridge,
        # whose drain tags the UI envelope with the CHILD run id.
        child_inv = InvocationId(f"inv_{uuid4()}")
        observer(WorkflowRunStartedEvent(workflow_run_id=child, trace_id=tr))
        observer(
            WorkflowNodeStartedEvent(
                workflow_run_id=child,
                trace_id=tr,
                invocation_id=child_inv,
                node_name="child_node",
            )
        )

        # Let the child drain process the buffered items.
        for _ in range(40):
            await asyncio.sleep(0.02)
            progress = await _collect_progress(store, cards._thread_id)
            child_progress = [p for p in progress if p.get("run_id") == str(child)]
            if len(child_progress) >= 2:
                break

        child_progress = [
            p
            for p in await _collect_progress(store, cards._thread_id)
            if p.get("run_id") == str(child)
        ]
        kinds = {p["kind"] for p in child_progress}
        assert "workflow.run.started" in kinds
        assert "workflow.node.started" in kinds
        # None of the child events leaked onto the parent's run_id.
        assert all(p["run_id"] == str(child) for p in child_progress)
        # The child's node event carries the child node name.
        node_started = next(
            p for p in child_progress if p["kind"] == "workflow.node.started"
        )
        assert node_started.get("node_name") == "child_node"
    finally:
        await impl.aclose()
        await cards.aclose()


# ---------------------------------------------------------------------------
# T6 — child lane shuts down cleanly on completion.
# ---------------------------------------------------------------------------


async def test_child_lane_shuts_down_on_completion(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk(tmp_path, store)
    try:
        parent = WorkflowRunId(f"wrk_{uuid4()}")
        child = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")
        impl._workflow_registry[parent] = _record(parent, ref="b/workflow/parent")
        parent_queue: asyncio.Queue[Any] = asyncio.Queue()
        impl._workflow_bridges[parent] = parent_queue

        observer = impl._make_dispatch_event_observer(
            parent, impl._workflow_registry[parent]
        )
        called = _called_workflow_event(
            parent_run_id=parent,
            trace_id=tr,
            child_run_id=child,
            child_ref="b/workflow/child",
        )
        observer(called)
        await impl._open_child_lane(parent, called.payload)
        child_card = impl._workflow_registry[child].card
        child_task = impl._per_run_drain_tasks[child]

        # The child's own terminal RUN_COMPLETED (through the child
        # bridge) closes the child card with success.
        observer(WorkflowRunCompletedEvent(workflow_run_id=child, trace_id=tr))

        # Let the child's RUN_COMPLETED land first (closes the card).
        child_queue = impl._workflow_bridges.get(child)
        assert child_queue is not None
        for _ in range(40):
            await asyncio.sleep(0.02)
            if impl._workflow_registry[child].phase == "success":
                break

        # The parent drain, on CALLED_WORKFLOW_COMPLETED, posts the stop
        # sentinel to the child bridge (spec-103). We post it directly
        # here (same mechanism) to end the child drain loop.
        child_queue.put_nowait(_STOP_SENTINEL)

        # The child drain completes (sentinel processed) and self-cleans.
        await asyncio.wait_for(child_task, timeout=2.0)
        assert child_task.done()
        assert child not in impl._per_run_drain_tasks
        assert child not in impl._workflow_bridges

        # Child card closed with success.
        assert child_card.is_closed
        assert impl._workflow_registry[child].phase == "success"
    finally:
        await impl.aclose()
        await cards.aclose()


# ---------------------------------------------------------------------------
# T4/T6 (integration) — the real parent drain opens the child lane on
# CALLED_WORKFLOW and posts the child stop sentinel on
# CALLED_WORKFLOW_COMPLETED, exercising the drain-level code paths.
# ---------------------------------------------------------------------------


async def test_parent_drain_opens_and_closes_child_lane(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk(tmp_path, store)
    try:
        parent = WorkflowRunId(f"wrk_{uuid4()}")
        child = WorkflowRunId(f"wrk_{uuid4()}")
        tr = TraceId(f"tr_{uuid4()}")

        parent_card = await cards.open("parent", kind="workflow")
        parent_card._owner_transferred = True
        rec = _record(parent, ref="b/workflow/parent")
        rec.card = parent_card
        impl._workflow_registry[parent] = rec
        parent_queue: asyncio.Queue[Any] = asyncio.Queue()
        impl._workflow_bridges[parent] = parent_queue

        observer = impl._make_dispatch_event_observer(parent, rec)

        # Start the REAL parent drain.
        from cli.chat.services.workflows_impl import WorkflowRunSpec

        parent_spec = WorkflowRunSpec(
            run_id=parent, ref="b/workflow/parent", inputs={}, card=parent_card
        )
        loop = asyncio.get_running_loop()
        parent_task = loop.create_task(impl._drain_workflow_bridge(parent_spec, None))

        # Emit the parent's stream through the dispatch observer: it
        # buffers into the parent bridge (home fallback) and the child
        # bridge (race-guarded on CALLED_WORKFLOW).
        observer(WorkflowRunStartedEvent(workflow_run_id=parent, trace_id=tr))
        called = _called_workflow_event(
            parent_run_id=parent,
            trace_id=tr,
            child_run_id=child,
            child_ref="b/workflow/child",
        )
        observer(called)
        # Child stream — routes to the child bridge.
        observer(WorkflowRunStartedEvent(workflow_run_id=child, trace_id=tr))
        observer(WorkflowRunCompletedEvent(workflow_run_id=child, trace_id=tr))
        # Parent bookend — the parent drain posts the child stop sentinel.
        observer(
            _called_completed_event(
                parent_run_id=parent,
                trace_id=tr,
                child_run_id=child,
                child_status="succeeded",
            )
        )

        # Wait for the child lane to open + shut down.
        child_task = None
        for _ in range(80):
            await asyncio.sleep(0.02)
            child_task = impl._per_run_drain_tasks.get(child)
            if child in impl._workflow_registry and child not in impl._workflow_bridges:
                break

        # Child lane opened as a peer with parent attribution.
        child_rec = impl._workflow_registry.get(child)
        assert child_rec is not None
        assert child_rec.parent_run_id == parent
        assert child in impl._children_of[parent]
        assert child_rec.phase == "success"
        assert child_rec.card is not None
        assert child_rec.card.is_closed
        # Child drain self-cleaned (sentinel processed) — no leak.
        assert child not in impl._per_run_drain_tasks
        assert child not in impl._workflow_bridges
        if child_task is not None:
            assert child_task.done()

        # Stop the parent drain.
        parent_queue.put_nowait(_STOP_SENTINEL)
        await asyncio.wait_for(parent_task, timeout=2.0)
    finally:
        await impl.aclose()
        await cards.aclose()
