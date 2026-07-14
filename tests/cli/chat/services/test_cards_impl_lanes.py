"""spec-006 Test 5 — Two-lane drop policy.

Three passes:

1. Fill the bounded queue (256 slots) with streaming appends; more
   appends increment `_dropped_events` (streaming lane =
   `put_nowait` with drop counter).
2. `open` and `close` on a card while the queue is saturated: BOTH
   succeed (critical lane — blocking put). No drop count increment.
3. `snapshot` while queue is saturated: succeeds (critical lane).

We stub `_publish_to_bus` with a suspending future so the drain
worker parks after popping one event; every subsequent event lands
in the queue instead of being consumed.

Snapshot-N / -MS are set to sky-high values so the streaming appends
don't trigger a critical-lane snapshot mid-stream (which would
deadlock against a saturated queue).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest

from cli.chat.services.cards_impl import CardsImpl
from schemas.events import Event
from store import SessionStore, SqliteBackend


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


async def _mk_impl_paused(store: SessionStore) -> CardsImpl:
    """Build a CardsImpl whose drain never advances.

    Replace `_publish_to_bus` with an awaitable that blocks forever
    so the drain worker parks on the first event and every subsequent
    event lands in the queue instead of being consumed.
    """
    session = await store.create_session("ag_test", name="c")
    threads = await store.list_threads(session.session_id)
    impl = CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )
    park = asyncio.Event()

    async def _park(event: Event) -> None:
        _ = event
        await park.wait()

    impl._publish_to_bus = _park  # type: ignore[assignment]
    return impl


async def test_streaming_saturates_and_drops(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    # Disable snapshot coalescing so the appends never fire a critical
    # snapshot (which would deadlock against a saturated queue).
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "10000000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "3600000")
    impl = await _mk_impl_paused(store)
    try:
        card = await impl.open("t", kind="markdown")

        # The drain parks on the first event it pops. To force drops,
        # push enough appends to overflow the 256-slot queue (one slot
        # is 'held' by the parked drain-in-flight event, so effectively
        # we need > 256 puts after the drain has taken one).
        for i in range(300):
            await card.append(f"a{i}")

        # At least one drop must have happened.
        assert impl._dropped_events > 0
    finally:
        await impl.aclose()


async def test_critical_open_close_while_queue_full(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "10000000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "3600000")
    impl = await _mk_impl_paused(store)
    try:
        # Spin up drain via first open, saturate with streaming appends.
        card = await impl.open("t", kind="markdown")
        for _ in range(600):
            await card.append("x")
        assert impl._dropped_events > 0
        pre_drop = impl._dropped_events

        # Critical open/close blocks on a full queue. We free slots by
        # popping in parallel — the critical put resumes as soon as a
        # slot opens, no drop counter increment.
        async def _do_open_close() -> None:
            card2 = await impl.open("t2", kind="markdown")
            await card2.close()

        do_task = asyncio.create_task(_do_open_close())
        # Yield so the critical put can park on the full queue.
        await asyncio.sleep(0.010)
        # Manually pop enough slots that both the open + close puts
        # can succeed.
        for _ in range(50):
            try:
                impl._stream_queue.get_nowait()
                impl._stream_queue.task_done()
            except asyncio.QueueEmpty:
                break
            await asyncio.sleep(0)
        await asyncio.wait_for(do_task, timeout=2.0)

        # No drop happened on the critical lane.
        assert impl._dropped_events == pre_drop
    finally:
        await impl.aclose()


async def test_snapshot_critical_lane_never_drops(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "10000000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "3600000")
    impl = await _mk_impl_paused(store)
    try:
        card = await impl.open("t", kind="markdown")
        for _ in range(600):
            await card.append("x")
        assert impl._dropped_events > 0
        pre_drop = impl._dropped_events

        # Trigger a snapshot emit in parallel; free queue slots so it
        # can complete.
        snapshot_task = asyncio.create_task(card._emit_snapshot())
        await asyncio.sleep(0.010)
        for _ in range(50):
            try:
                impl._stream_queue.get_nowait()
                impl._stream_queue.task_done()
            except asyncio.QueueEmpty:
                break
            await asyncio.sleep(0)
        await asyncio.wait_for(snapshot_task, timeout=2.0)

        assert impl._dropped_events == pre_drop
    finally:
        await impl.aclose()
