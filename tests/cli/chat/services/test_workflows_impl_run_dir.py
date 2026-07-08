"""spec-012 Test 7 — `run_dir` populated after executor construction.

Two passes:

1. After `start()` and executor construction, `record.run_dir` is a
   `RunDirectory` instance (not None).
2. `cancel()` writes to `record.run_dir.cancel_flag_path` — not a
   None dereference.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.fakes import (
    FakeExecutorSpec,
    install_fake_executor_factory,
)
from workflows.events import (
    WorkflowRunCompletedEvent,
    WorkflowRunStartedEvent,
)
from workflows.ids import TraceId, WorkflowRunId
from workflows.run import RunDirectory
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


def _slow_events() -> list:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    tr = TraceId(f"tr_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=tr),
    ]


async def test_run_dir_populated_after_construction(
    tmp_path: Path, store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        install_fake_executor_factory(
            impl,
            {
                "kolsch/workflows/x": FakeExecutorSpec(
                    events=_slow_events(), per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/x", {})
        # Wait until executor built and run_dir stamped on the record.
        for _ in range(60):
            await asyncio.sleep(0.05)
            rec = impl._workflow_registry.get(rid)
            if rec is not None and rec.run_dir is not None:
                break
        rec = impl._workflow_registry[rid]
        assert isinstance(rec.run_dir, RunDirectory)
    finally:
        await impl.aclose()


async def test_cancel_writes_to_populated_run_dir(
    tmp_path: Path, store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        install_fake_executor_factory(
            impl,
            {
                "kolsch/workflows/x": FakeExecutorSpec(
                    events=_slow_events(), per_event_delay=0.5,
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/x", {})
        for _ in range(60):
            await asyncio.sleep(0.05)
            rec = impl._workflow_registry.get(rid)
            if rec is not None and rec.run_dir is not None:
                break
        rec = impl._workflow_registry[rid]
        assert rec.run_dir is not None

        ok = await impl.cancel(rid)
        assert ok is True
        # File-based cancel flag on disk.
        assert rec.run_dir.cancel_flag_path.exists()
        assert rec.run_dir.cancel_flag_path.read_text() == "user_cancel"
    finally:
        await impl.aclose()
