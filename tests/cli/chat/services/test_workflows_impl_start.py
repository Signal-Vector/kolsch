"""spec-012 Test 1 — `start` + FIFO queue serialisation.

Three passes:

1. Two `start()` calls in rapid succession return distinct
   `WorkflowRunId`s immediately (non-blocking).
2. Mock executor for run A takes ~500ms; run B's phase stays
   `"queued"` until A completes; then B transitions to `"running"`.
3. Registry has both records at appropriate phases through the run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.events import (
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import TraceId, WorkflowRunId
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


async def _mk_impl(
    tmp_path: Path,
    store: SessionStore,
) -> WorkflowsImpl:
    session = await store.create_session("ag_test", name="w")
    threads = await store.list_threads(session.session_id)
    run_store = WorkflowRunStore(tmp_path)
    return WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=run_store,
    )


def _mk_events_for(run_id: str) -> tuple[list, TraceId]:
    from uuid import uuid4
    trace = TraceId(f"tr_{uuid4()}")
    rid = WorkflowRunId(run_id)
    return (
        [
            WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=trace),
            WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=trace),
        ],
        trace,
    )


async def test_two_starts_return_distinct_ids_immediately(
    tmp_path: Path, store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        events, _ = _mk_events_for(f"wrk_{'0' * 8}-0000-4000-8000-{'0' * 12}")
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/a": MockExecutorSpec(
                    events=events, per_event_delay=0.1,
                ),
                "kolsch/workflows/b": MockExecutorSpec(events=events),
            },
            runs_root=tmp_path / "runs",
        )
        id_a = await impl.start("kolsch/workflows/a", {})
        id_b = await impl.start("kolsch/workflows/b", {})
        assert id_a != id_b
        assert str(id_a).startswith("wrk_")
        assert str(id_b).startswith("wrk_")
    finally:
        await impl.aclose()


async def test_second_run_stays_queued_until_first_completes(
    tmp_path: Path, store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        events, _ = _mk_events_for(f"wrk_{'0' * 8}-0000-4000-8000-{'0' * 12}")
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/a": MockExecutorSpec(
                    events=events, per_event_delay=0.25,  # ~0.5s total
                ),
                "kolsch/workflows/b": MockExecutorSpec(events=events),
            },
            runs_root=tmp_path / "runs",
        )
        id_a = await impl.start("kolsch/workflows/a", {})
        id_b = await impl.start("kolsch/workflows/b", {})
        # Give the drain a moment to pick up A. B should still be queued.
        await asyncio.sleep(0.05)
        status_b = impl.status(id_b)
        assert status_b.phase == "queued", (
            f"B should still be queued while A runs; got {status_b.phase}"
        )
        # Wait for A to finish.
        for _ in range(60):
            await asyncio.sleep(0.05)
            if impl.status(id_a).phase == "success":
                break
        assert impl.status(id_a).phase == "success"
        # B eventually runs to completion too.
        for _ in range(30):
            await asyncio.sleep(0.05)
            if impl.status(id_b).phase == "success":
                break
        assert impl.status(id_b).phase == "success"
    finally:
        await impl.aclose()


async def test_registry_carries_both_records_during_run(
    tmp_path: Path, store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        events, _ = _mk_events_for(f"wrk_{'0' * 8}-0000-4000-8000-{'0' * 12}")
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/a": MockExecutorSpec(
                    events=events, per_event_delay=0.20,
                ),
                "kolsch/workflows/b": MockExecutorSpec(events=events),
            },
            runs_root=tmp_path / "runs",
        )
        id_a = await impl.start("kolsch/workflows/a", {})
        id_b = await impl.start("kolsch/workflows/b", {})
        # Both must appear in the registry immediately.
        assert id_a in impl._workflow_registry
        assert id_b in impl._workflow_registry
        # A should be either queued or running quickly; B should be queued.
        await asyncio.sleep(0.05)
        rec_a = impl._workflow_registry[id_a]
        rec_b = impl._workflow_registry[id_b]
        assert rec_a.phase in ("queued", "running")
        assert rec_b.phase == "queued"
    finally:
        await impl.aclose()
