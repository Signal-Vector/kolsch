"""spec-012 Test 5 — Observer + bridge drain (WIRE_WORKFLOW_PROGRESS).

Three passes:

1. Mock executor emits 5 events (RUN_STARTED, 3× NODE_*,
   RUN_COMPLETED). Observer enqueues each into the bridge; bridge
   drain fires 5 `kolsch.workflow.progress` UI events plus a
   `card.closed` on terminal.
2. Bound card closes with `final_status="success"` on RUN_COMPLETED.
3. Bound card closes with `final_status="error"` on RUN_FAILED and
   `final_status="cancelled"` on RUN_ABORTED.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.workflows_impl import WorkflowsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_CARD_CLOSED, WIRE_WORKFLOW_PROGRESS
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.events import (
    WorkflowNodeCompletedEvent,
    WorkflowNodeStartedEvent,
    WorkflowRunAbortedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunFailedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import InvocationId, TraceId, WorkflowRunId
from workflows.run_store import WorkflowRunStore


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


def _five_events() -> list:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    tr = TraceId(f"tr_{uuid4()}")
    inv = InvocationId(f"inv_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr),
        WorkflowNodeStartedEvent(
            workflow_run_id=rid,
            trace_id=tr,
            invocation_id=inv,
            node_name="n1",
        ),
        WorkflowNodeCompletedEvent(
            workflow_run_id=rid,
            trace_id=tr,
            invocation_id=inv,
            node_name="n1",
        ),
        WorkflowNodeStartedEvent(
            workflow_run_id=rid,
            trace_id=tr,
            invocation_id=inv,
            node_name="n2",
        ),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=tr),
    ]


async def _collect(
    store: SessionStore,
    thread_id: str,
    kind: str,
) -> list[dict]:
    out: list[dict] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == kind:
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                out.append(payload)
    return out


async def _wait_terminal(impl: WorkflowsImpl, rid) -> None:
    for _ in range(80):
        await asyncio.sleep(0.05)
        phase = impl.status(rid).phase
        if phase in ("success", "error", "cancelled"):
            return


async def test_bridge_emits_progress_events_and_closes_card(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {"kolsch/workflows/x": MockExecutorSpec(events=_five_events())},
            runs_root=tmp_path / "runs",
        )
        card = await cards.open("wf", kind="workflow")
        rid = await impl.start("kolsch/workflows/x", {}, card=card)
        await _wait_terminal(impl, rid)
        # Small tail wait so the card.closed emit lands.
        await asyncio.sleep(0.1)

        progress = await _collect(
            store,
            cards._thread_id,
            WIRE_WORKFLOW_PROGRESS,
        )
        assert len(progress) == 5, f"expected 5 progress events, got {len(progress)}"
        for p in progress:
            assert p["run_id"] == str(rid)

        closed = await _collect(store, cards._thread_id, WIRE_CARD_CLOSED)
        assert len(closed) == 1
        assert closed[0]["final_status"] == "success"
    finally:
        await impl.aclose()
        await cards.aclose()


async def test_card_closes_with_error_on_run_failed(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk(tmp_path, store)
    try:
        rid_e = WorkflowRunId(f"wrk_{uuid4()}")
        tr_e = TraceId(f"tr_{uuid4()}")
        events = [
            WorkflowRunStartedEvent(workflow_run_id=rid_e, trace_id=tr_e),
            WorkflowRunFailedEvent(workflow_run_id=rid_e, trace_id=tr_e),
        ]
        install_mock_executor_factory(
            impl,
            {"kolsch/workflows/f": MockExecutorSpec(events=events)},
            runs_root=tmp_path / "runs",
        )
        card = await cards.open("wf", kind="workflow")
        rid = await impl.start("kolsch/workflows/f", {}, card=card)
        await _wait_terminal(impl, rid)
        await asyncio.sleep(0.1)
        closed = await _collect(store, cards._thread_id, WIRE_CARD_CLOSED)
        assert len(closed) == 1
        assert closed[0]["final_status"] == "error"
    finally:
        await impl.aclose()
        await cards.aclose()


async def test_card_closes_with_cancelled_on_run_aborted(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl, cards = await _mk(tmp_path, store)
    try:
        rid_a = WorkflowRunId(f"wrk_{uuid4()}")
        tr_a = TraceId(f"tr_{uuid4()}")
        events = [
            WorkflowRunStartedEvent(workflow_run_id=rid_a, trace_id=tr_a),
            WorkflowRunAbortedEvent(
                workflow_run_id=rid_a,
                trace_id=tr_a,
                payload={"reason": "user_cancel"},
            ),
        ]
        install_mock_executor_factory(
            impl,
            {"kolsch/workflows/a": MockExecutorSpec(events=events)},
            runs_root=tmp_path / "runs",
        )
        card = await cards.open("wf", kind="workflow")
        rid = await impl.start("kolsch/workflows/a", {}, card=card)
        await _wait_terminal(impl, rid)
        await asyncio.sleep(0.1)
        closed = await _collect(store, cards._thread_id, WIRE_CARD_CLOSED)
        assert len(closed) == 1
        assert closed[0]["final_status"] == "cancelled"
    finally:
        await impl.aclose()
        await cards.aclose()
