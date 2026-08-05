"""feat-127 — `WorkflowsImpl.retire_live_for_switch` (the `/new` retirement).

Retires all live workflow work for a `/new` session switch WITHOUT closing the
impl: discards queued specs, cancels every non-terminal run, drives every drain
to `done()`, and leaves a durably-suspended `user_input` run untouched — while
staying usable for the new session.

Coverage:

1. Cancels a live run, drives it terminal, and the impl still starts workflows
   afterwards (no `_closed`; the FIFO drain survives). Per-run trackers +
   `_children_of` are cleared.
2. Discards a QUEUED run so it never promotes into the new session.
3. Leaves a durably-suspended run (`waiting_at` sidecar) untouched — no
   `cancel.flag`, not cancelled.
4. Idle impl → no-op (returns 0), still usable.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
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
from workflows.run import RunDirectory
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


def _completing_events() -> list[WorkflowEvent]:
    rid = WorkflowRunId(f"wrk_{uuid4()}")
    tr = TraceId(f"tr_{uuid4()}")
    return [
        WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=tr),
        WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=tr),
    ]


async def _wait_running(impl: WorkflowsImpl, rid: WorkflowRunId) -> None:
    for _ in range(60):
        await asyncio.sleep(0.03)
        rec = impl._workflow_registry.get(rid)
        if rec is not None and rec.run_dir is not None and rec.phase == "running":
            return


async def _wait_terminal(impl: WorkflowsImpl, rid: WorkflowRunId) -> str:
    for _ in range(100):
        await asyncio.sleep(0.03)
        phase = impl.status(rid).phase
        if phase in ("success", "error", "cancelled"):
            return phase
    return impl.status(rid).phase


async def test_retire_cancels_live_and_keeps_impl_usable(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/slow": MockExecutorSpec(
                    events=_completing_events(),
                    per_event_delay=0.5,
                ),
                "kolsch/workflows/after": MockExecutorSpec(
                    events=_completing_events(),
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/slow", {})
        await _wait_running(impl, rid)

        cancelled = await impl.retire_live_for_switch(timeout=3.0)
        assert cancelled == 1
        # The live run reached a terminal phase.
        assert impl.status(rid).phase in ("cancelled", "error")
        # Trackers cleared (incl. the `_children_of` leak).
        assert impl._children_of == {}
        assert impl._per_run_drain_tasks == {}
        assert impl._closed is False

        # The impl is still usable — a workflow starts in the "new session".
        rid2 = await impl.start("kolsch/workflows/after", {})
        assert await _wait_terminal(impl, rid2) == "success"
    finally:
        await impl.aclose()


async def test_retire_discards_queued(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        install_mock_executor_factory(
            impl,
            {
                # `head` runs slowly so `q1`/`q2` stay QUEUED behind it.
                "kolsch/workflows/head": MockExecutorSpec(
                    events=_completing_events(),
                    per_event_delay=0.5,
                ),
                "kolsch/workflows/q1": MockExecutorSpec(events=_completing_events()),
                "kolsch/workflows/q2": MockExecutorSpec(events=_completing_events()),
            },
            runs_root=tmp_path / "runs",
        )
        head = await impl.start("kolsch/workflows/head", {})
        await _wait_running(impl, head)
        q1 = await impl.start("kolsch/workflows/q1", {})
        q2 = await impl.start("kolsch/workflows/q2", {})
        assert impl.status(q1).phase == "queued"
        assert impl.status(q2).phase == "queued"

        await impl.retire_live_for_switch(timeout=3.0)

        # The queued runs were discarded (never promoted) — gone from registry.
        assert q1 not in impl._workflow_registry
        assert q2 not in impl._workflow_registry
        # Let the event loop settle: neither queued run ever ran.
        await asyncio.sleep(0.1)
        assert impl.status(q1).phase in ("unknown", "error")
        assert impl.status(q2).phase in ("unknown", "error")
    finally:
        await impl.aclose()


async def test_retire_leaves_durably_suspended(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """A `waiting_for_input`-suspended run must NOT be cancelled by retire."""
    impl = await _mk_impl(tmp_path, store)
    try:
        # A finalized-but-suspended run: real run dir with a `waiting_at`
        # sidecar, still shown `running` in the registry (the narrow window).
        run_dir = RunDirectory(root=tmp_path / "runs" / f"wrk_{uuid4()}")
        run_dir.root.mkdir(parents=True, exist_ok=True)
        run_dir.waiting_at_path.write_text("inv_x", encoding="utf-8")
        rid = WorkflowRunId(run_dir.root.name)
        impl._workflow_registry[rid] = SimpleNamespace(  # type: ignore[assignment]
            phase="running",
            run_dir=run_dir,
            card=None,
        )

        cancelled = await impl.retire_live_for_switch(timeout=1.0)

        assert cancelled == 0  # the suspended run was skipped
        assert not run_dir.cancel_flag_path.exists()  # never cancelled on disk
        # Still present in-memory (rebind_session, not retire, clears the
        # registry); its disk state is untouched → resumable.
        assert rid in impl._workflow_registry
    finally:
        await impl.aclose()


async def test_retire_idle_is_noop(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_impl(tmp_path, store)
    try:
        cancelled = await impl.retire_live_for_switch(timeout=1.0)
        assert cancelled == 0
        assert impl._closed is False
    finally:
        await impl.aclose()


async def test_retire_bounded_when_run_swallows_cancel(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    """The force path must stay bounded against a cancel-swallowing run.

    A node body can swallow its ``CancelledError`` (``except
    BaseException`` in user code); Python cannot force-kill such a
    coroutine. Retire's force path must time out its waits, re-arm the
    FIFO drain, and return — an unbounded await there would hang `/new`
    inside its own safety net. The impl must stay usable afterwards.
    """
    impl = await _mk_impl(tmp_path, store)
    old_drain: asyncio.Task[None] | None = None
    try:

        async def _swallow_once(_run_id: str) -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # A node body that eats the cancel and parks again.
                await asyncio.sleep(3600)

        install_mock_executor_factory(
            impl,
            {
                "kolsch/workflows/wedged": MockExecutorSpec(
                    events=_completing_events(),
                    block_on=_swallow_once,
                ),
                "kolsch/workflows/after": MockExecutorSpec(
                    events=_completing_events(),
                ),
            },
            runs_root=tmp_path / "runs",
        )
        rid = await impl.start("kolsch/workflows/wedged", {})
        await _wait_running(impl, rid)
        old_drain = impl._workflow_drain_task
        assert old_drain is not None

        # Bounded end-to-end even though the run never dies: cooperative
        # wait (0.2s) + force wait (min(1.0, 0.2)) — 5s is generous.
        await asyncio.wait_for(
            impl.retire_live_for_switch(timeout=0.2),
            timeout=5.0,
        )

        # The FIFO drain was re-armed; the impl still runs workflows.
        assert impl._closed is False
        rid2 = await impl.start("kolsch/workflows/after", {})
        assert await _wait_terminal(impl, rid2) == "success"
    finally:
        # Put the orphaned swallower down (second cancel propagates —
        # only the first was swallowed) so no task outlives the loop.
        if old_drain is not None and not old_drain.done():
            old_drain.cancel()
            await asyncio.wait({old_drain}, timeout=2.0)
        await impl.aclose()
