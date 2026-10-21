"""bug-140 — a cancel delivered while a run finalizes must not be swallowed.

`_finalize_terminal` awaits the run's bridge-drain task. If the shared
FIFO drain task is cancelled at that moment (e.g. `aclose()` tearing
down), the CancelledError surfaces at `await bridge_task`. The old code
suppressed it there, `_run_one_workflow` returned normally, and the
drain looped back to `queue.get()` with the cancel already consumed —
so `aclose()`'s `await self._workflow_drain_task` waited forever (the
intermittent xdist hang in `test_parent_populated` /
`test_second_run_stays_queued_until_first_completes`).

The interleaving is driven deterministically with events — no sleeps:
the bridge drain parks on an event, `_finalize_terminal` signals when
it is entered, and the drain task is cancelled while it awaits the
bridge.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from cli.chat.services.workflows_impl import WorkflowsImpl
from store import SessionStore, SqliteBackend
from tests.cli.chat.services.mocks import (
    MockExecutorSpec,
    install_mock_executor_factory,
)
from workflows.events import WorkflowRunCompletedEvent, WorkflowRunStartedEvent
from workflows.ids import TraceId, WorkflowRunId
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

# Generous bound: these waits only time out when the bug is present.
_BOUND = 10.0


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


async def _impl_parked_in_finalize(
    tmp_path: Path, store: SessionStore
) -> tuple[WorkflowsImpl, asyncio.Event]:
    """Start one run and return once the drain is parked in finalize.

    The run's bridge drain is replaced by a coroutine that never
    finishes on its own, so `_finalize_terminal` blocks at
    `await bridge_task`.
    """
    session = await store.create_session("ag_test", name="w")
    (thread,) = await store.list_threads(session.session_id)
    impl = WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread.thread_id,
        run_store=WorkflowRunStore(tmp_path),
    )
    rid = WorkflowRunId(f"wrk_{'0' * 8}-0000-4000-8000-{'0' * 12}")
    trace = TraceId(f"tr_{'1' * 8}-0000-4000-8000-{'1' * 12}")
    install_mock_executor_factory(
        impl,
        {
            "kolsch/workflows/a": MockExecutorSpec(
                events=[
                    WorkflowRunStartedEvent(workflow_run_id=rid, trace_id=trace),
                    WorkflowRunCompletedEvent(workflow_run_id=rid, trace_id=trace),
                ]
            )
        },
        runs_root=tmp_path / "runs",
    )

    never = asyncio.Event()

    async def _parked_bridge(*_a: Any, **_k: Any) -> None:
        await never.wait()

    finalizing = asyncio.Event()
    real_finalize = impl._finalize_terminal

    async def _finalize(*a: Any, **k: Any) -> None:
        finalizing.set()
        await real_finalize(*a, **k)

    impl._drain_workflow_bridge = _parked_bridge  # type: ignore[method-assign]
    impl._finalize_terminal = _finalize  # type: ignore[method-assign]

    await impl.start("kolsch/workflows/a", {})
    await asyncio.wait_for(finalizing.wait(), timeout=_BOUND)
    # One loop turn: the drain runs from `finalizing.set()` until it
    # suspends at `await bridge_task`.
    await asyncio.sleep(0)
    return impl, never


async def test_drain_cancel_during_finalize_is_not_swallowed(
    tmp_path: Path, store: SessionStore
) -> None:
    impl, never = await _impl_parked_in_finalize(tmp_path, store)
    drain = impl._workflow_drain_task
    assert drain is not None
    assert not drain.done()
    try:
        drain.cancel()
        done, _ = await asyncio.wait({drain}, timeout=_BOUND)
        assert drain in done, (
            "drain task swallowed its own cancellation and kept running "
            "(parked on the FIFO queue) — aclose() would wait forever"
        )
        assert drain.cancelled()
    finally:
        never.set()
        await asyncio.wait_for(impl.aclose(), timeout=_BOUND)


async def test_aclose_while_run_finalizing_returns(
    tmp_path: Path, store: SessionStore
) -> None:
    # Guards the `_closed` early return in the drain loop. Not a
    # reproducer: `aclose()` cancels the bridge before the drain, so
    # this ordering passed before bug-140 too.
    impl, never = await _impl_parked_in_finalize(tmp_path, store)
    try:
        await asyncio.wait_for(impl.aclose(), timeout=_BOUND)
        assert impl._workflow_drain_task is not None
        assert impl._workflow_drain_task.done()
    finally:
        never.set()
