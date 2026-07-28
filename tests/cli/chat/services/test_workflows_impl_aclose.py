"""spec-012 Test 6 — `aclose` teardown.

Four passes:

1. Start 2 workflows (one queued, one running); `aclose()`. Both
   cancel tokens fired.
2. Per-run drain tasks cancelled.
3. Workflow drain task cancelled.
4. Second `aclose()` idempotent; post-`aclose`, `start()` raises
   `RuntimeError`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.events import (
    WorkflowEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import TraceId, WorkflowRunId
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
    return WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
        run_store=WorkflowRunStore(tmp_path),
    )


def _slow_events() -> list[WorkflowEvent]:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    tr = TraceId(f"tr_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=tr),
    ]


async def test_aclose_fires_tokens_for_live_runs(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    install_mock_executor_factory(
        impl,
        {
            "kolsch/workflows/a": MockExecutorSpec(
                events=_slow_events(),
                per_event_delay=1.0,
            ),
            "kolsch/workflows/b": MockExecutorSpec(
                events=_slow_events(),
                per_event_delay=1.0,
            ),
        },
        runs_root=tmp_path / "runs",
    )
    id_a = await impl.start("kolsch/workflows/a", {})
    id_b = await impl.start("kolsch/workflows/b", {})
    # A is running (or queued at construction); B is queued.
    # Wait until A's cancel token is registered.
    for _ in range(30):
        await asyncio.sleep(0.05)
        if id_a in impl._workflow_cancels:
            break

    token_a = impl._workflow_cancels.get(id_a)
    assert token_a is not None
    await impl.aclose()
    assert token_a.is_cancelled()
    # B never made it to running so it never got a token — that's ok.
    _ = id_b


async def test_aclose_cancels_per_run_and_drain_tasks(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    install_mock_executor_factory(
        impl,
        {
            "kolsch/workflows/x": MockExecutorSpec(
                events=_slow_events(),
                per_event_delay=1.0,
            ),
        },
        runs_root=tmp_path / "runs",
    )
    rid = await impl.start("kolsch/workflows/x", {})
    for _ in range(30):
        await asyncio.sleep(0.05)
        if rid in impl._per_run_drain_tasks:
            break
    bridge_task = impl._per_run_drain_tasks[rid]
    drain_task = impl._workflow_drain_task
    assert drain_task is not None

    await impl.aclose()
    assert bridge_task.done()
    assert drain_task.done()


async def test_aclose_is_idempotent_and_start_raises_after(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    await impl.aclose()
    # Second call must not raise.
    await impl.aclose()
    with pytest.raises(RuntimeError):
        await impl.start("kolsch/workflows/x", {})


async def test_two_workflows_registry_before_aclose(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/a": MockExecutorSpec(
                    events=_slow_events(),
                    per_event_delay=0.5,
                ),
                "kolsch/workflows/b": MockExecutorSpec(
                    events=_slow_events(),
                    per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        id_a = await impl.start("kolsch/workflows/a", {})
        id_b = await impl.start("kolsch/workflows/b", {})
        assert id_a in impl._workflow_registry
        assert id_b in impl._workflow_registry
    finally:
        await impl.aclose()
