"""spec-012 Test 2 — `status` in-memory (registry) read.

Three passes:

1. `status(unknown_id)` returns `phase="unknown"` (no registry + no
   on-disk record).
2. `status(mid-run_id)` returns `phase="running"` while the run is
   in flight; node counters are populated.
3. `status(60s-after-terminal_id)` returns `phase="unknown"` — the
   registry eviction dropped the record.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.events import (
    WorkflowNodeCompletedEvent,
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


async def _mk_impl(tmp_path: Path, store: SessionStore) -> WorkflowsImpl:
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


def _mk_events_with_node() -> list:
    from uuid import uuid4

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
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=tr),
    ]


async def test_status_unknown_returns_unknown_phase(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        from uuid import uuid4

        stray = WorkflowRunId(f"wrk_{uuid4()}")
        result = impl.status(stray)
        assert result.phase == "unknown"
        assert result.run_id == stray
    finally:
        await impl.aclose()


async def test_status_mid_run_shows_running_and_counters(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/x": MockExecutorSpec(
                    events=_mk_events_with_node(),
                    per_event_delay=0.15,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/x", {})
        # Wait until the executor transitions the record to running
        # AND has emitted the NODE_STARTED event.
        for _ in range(40):
            await asyncio.sleep(0.05)
            rec = impl._workflow_registry.get(rid)
            if (
                rec is not None
                and rec.phase == "running"
                and (rec.node_counts.total >= 1)
            ):
                break
        result = impl.status(rid)
        assert result.phase == "running", f"expected running, got {result.phase}"
        assert result.node_counts is not None
        assert result.node_counts.total >= 1
    finally:
        await impl.aclose()


async def test_status_after_eviction_returns_unknown(
    tmp_path: Path,
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Short-circuit the 60s eviction: monkeypatch the constant.
    from cli.chat.services import workflows_impl as wi_mod

    monkeypatch.setattr(wi_mod, "_REGISTRY_EVICTION_SECONDS", 0.15)

    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/y": MockExecutorSpec(
                    events=_mk_events_with_node(),
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/y", {})
        # Wait for terminal.
        for _ in range(60):
            await asyncio.sleep(0.05)
            if impl.status(rid).phase == "success":
                break
        assert impl.status(rid).phase == "success"
        # Wait past the eviction window.
        await asyncio.sleep(0.35)
        # Registry-first miss; the on-disk run doesn't exist (mock
        # factory mints an empty run_dir but no meta.json), so cold-
        # read also returns unknown.
        assert impl.status(rid).phase == "unknown"
    finally:
        await impl.aclose()
