"""qol-143 — a cancel aimed at the caller of `aclose()` must propagate.

`WorkflowsImpl.aclose()` and `CardsImpl.aclose()` cancel a child task
and then await it. The old code wrapped that await in
`suppress(CancelledError)`, which also ate a cancel aimed at the task
running `aclose()`: the caller returned normally instead of ending
cancelled.

Each test parks a stubborn child — it catches the first cancel (the one
`aclose()` sends) and then waits on an event that never fires — so
`aclose()` is deterministically parked in its await. Cancelling the
closer forwards the cancel into the child, which then ends; the closer
must end cancelled. No timing sleeps: every wait is event-driven and
bounded only as a hang backstop.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from cli.chat.services._task_teardown import cancel_and_wait
from cli.chat.services.cards_impl import CardsImpl
from cli.chat.services.workflows_impl import WorkflowsImpl
from schemas.identifiers import EventId
from store import SessionStore, SqliteBackend
from workflows.ids import WorkflowRunId
from workflows.run_store import WorkflowRunStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine
    from pathlib import Path

# Generous bound: these waits only time out when something hangs.
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


async def _idle() -> None:
    """Park until cancelled."""
    await asyncio.Event().wait()


def _stubborn_child() -> tuple[Callable[[], Coroutine[Any, Any, None]], asyncio.Event]:
    """Return a child coroutine factory + an event set once it is parked.

    The child swallows its first cancel (the one `aclose()` sends) and
    parks on a never-set event, so the closer stays parked at its
    `await child` until a second cancel — the caller's, forwarded by
    asyncio — unwinds it. Only the first child from one factory to be
    cancelled parks; any sibling cancelled after that ends at once, so
    which sibling teardown reaches first does not matter.
    """
    parked = asyncio.Event()

    async def child() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if parked.is_set():
                raise
            parked.set()
            await asyncio.Event().wait()
            raise

    return child, parked


async def _cancel_closer_while_parked(
    aclose: Callable[[], Coroutine[Any, Any, None]],
    parked: asyncio.Event,
) -> asyncio.Task[None]:
    """Run `aclose()` in a task, cancel it once the child is parked."""
    closer = asyncio.create_task(aclose())
    await asyncio.wait_for(parked.wait(), _BOUND)
    assert not closer.done()
    closer.cancel()
    await asyncio.wait({closer}, timeout=_BOUND)
    assert closer.done()
    return closer


# ---------------------------------------------------------------------------
# WorkflowsImpl
# ---------------------------------------------------------------------------


async def _mk_workflows(tmp_path: Path, store: SessionStore) -> WorkflowsImpl:
    session = await store.create_session("ag_test", name="w")
    (thread,) = await store.list_threads(session.session_id)
    return WorkflowsImpl(
        runtime=None,
        cards=None,
        prompts=None,
        session_store=store,
        session_id=session.session_id,
        thread_id=thread.thread_id,
        run_store=WorkflowRunStore(tmp_path),
    )


async def test_workflows_aclose_caller_cancel_propagates(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    impl = await _mk_workflows(tmp_path, store)
    child, parked = _stubborn_child()
    drain = asyncio.create_task(child())
    impl._workflow_drain_task = drain

    closer = await _cancel_closer_while_parked(impl.aclose, parked)

    assert closer.cancelled()
    assert drain.done()


async def test_workflows_aclose_caller_cancel_in_gather_cancels_fifo_drain(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    # The closer is cancelled while parked in the per-run drain gather.
    # The cancel propagates, and the FIFO drain — not yet reached — is
    # still cancelled rather than leaked behind a closed impl.
    impl = await _mk_workflows(tmp_path, store)
    child, parked = _stubborn_child()
    bridge = asyncio.create_task(child())
    impl._per_run_drain_tasks[WorkflowRunId(f"wrk_{uuid4()}")] = bridge
    fifo = asyncio.create_task(_idle())
    impl._workflow_drain_task = fifo

    closer = await _cancel_closer_while_parked(impl.aclose, parked)

    assert closer.cancelled()
    assert bridge.done()
    await asyncio.wait({fifo}, timeout=_BOUND)
    assert fifo.cancelled()


async def test_workflows_aclose_without_caller_cancel_returns(
    tmp_path: Path,
    store: SessionStore,
) -> None:
    # The drain's own cancel is still swallowed: aclose returns normally.
    impl = await _mk_workflows(tmp_path, store)
    drain = asyncio.create_task(_idle())
    impl._workflow_drain_task = drain
    await asyncio.sleep(0)  # let the drain start and park

    await asyncio.wait_for(impl.aclose(), _BOUND)

    assert drain.cancelled()


# ---------------------------------------------------------------------------
# CardsImpl
# ---------------------------------------------------------------------------


async def _mk_cards(store: SessionStore) -> CardsImpl:
    session = await store.create_session("ag_test", name="c")
    (thread,) = await store.list_threads(session.session_id)
    return CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=thread.thread_id,
    )


async def test_cards_aclose_caller_cancel_propagates_from_drain(
    store: SessionStore,
) -> None:
    impl = await _mk_cards(store)
    child, parked = _stubborn_child()
    drain = asyncio.create_task(child())
    impl._drain_task = drain

    closer = await _cancel_closer_while_parked(impl.aclose, parked)

    assert closer.cancelled()
    assert drain.done()


async def test_cards_aclose_caller_cancel_propagates_from_notice_task(
    store: SessionStore,
) -> None:
    impl = await _mk_cards(store)
    child, parked = _stubborn_child()

    async def notice() -> EventId:
        await child()
        return EventId("evt_unreachable")

    # Two notice tasks: whichever the loop reaches first parks; the
    # other is never reached by the loop and must be cancelled by the
    # guard.
    notice_tasks = [asyncio.create_task(notice()) for _ in range(2)]
    for task in notice_tasks:
        impl._pending_tasks.add(task)
        task.add_done_callback(impl._pending_tasks.discard)
    drain = asyncio.create_task(_idle())
    impl._drain_task = drain

    closer = await _cancel_closer_while_parked(impl.aclose, parked)

    assert closer.cancelled()
    # Every remaining notice task and the drain worker are still
    # cancelled even though teardown was interrupted mid-loop — nothing
    # outlives a closed impl.
    await asyncio.wait(notice_tasks, timeout=_BOUND)
    assert sum(task.cancelled() for task in notice_tasks) == 2
    await asyncio.wait({drain}, timeout=_BOUND)
    assert drain.cancelled()


async def test_cards_aclose_notice_task_error_still_swallowed(
    store: SessionStore,
) -> None:
    # A notice task that turns its cancel into some other error is
    # swallowed, as before: aclose returns normally.
    impl = await _mk_cards(store)
    started = asyncio.Event()

    async def notice() -> EventId:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            msg = "boom during cancel"
            raise RuntimeError(msg) from None
        return EventId("evt_unreachable")

    notice_task = asyncio.create_task(notice())
    impl._pending_tasks.add(notice_task)
    await asyncio.wait_for(started.wait(), _BOUND)

    await asyncio.wait_for(impl.aclose(), _BOUND)

    assert isinstance(notice_task.exception(), RuntimeError)


# ---------------------------------------------------------------------------
# cancel_and_wait — the shared helper
# ---------------------------------------------------------------------------


async def test_cancel_and_wait_teardown_completes_while_unwinding_a_cancel() -> None:
    # A caller already unwinding its own cancel (cleanup in `except
    # CancelledError`) has `cancelling() > 0` on entry. That pre-existing
    # cancel is not a new one aimed at the teardown, so every child is
    # still waited for instead of aborting at the first.
    started = asyncio.Event()
    children = [asyncio.create_task(_idle()) for _ in range(2)]
    finished: list[bool] = []

    async def worker() -> None:
        try:
            started.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            for child in children:
                await cancel_and_wait(child)
            finished.append(True)
            raise

    task = asyncio.create_task(worker())
    await asyncio.wait_for(started.wait(), _BOUND)
    task.cancel()
    await asyncio.wait({task}, timeout=_BOUND)

    assert task.cancelled()
    assert finished == [True]
    assert all(child.cancelled() for child in children)


async def test_cancel_and_wait_error_propagates_unless_suppressed() -> None:
    started = asyncio.Event()

    async def failing() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            msg = "boom during cancel"
            raise RuntimeError(msg) from None

    child = asyncio.create_task(failing())
    await asyncio.wait_for(started.wait(), _BOUND)
    with pytest.raises(RuntimeError):
        await cancel_and_wait(child)

    started.clear()
    child = asyncio.create_task(failing())
    await asyncio.wait_for(started.wait(), _BOUND)
    await cancel_and_wait(child, suppress_errors=True)
    assert isinstance(child.exception(), RuntimeError)
