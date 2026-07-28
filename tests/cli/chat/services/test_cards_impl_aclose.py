"""spec-006 Test 7 — `CardsImpl.aclose`.

Four passes:

1. Open 3 cards; each has a pending snapshot timer scheduled.
   `aclose()`. Assert all 3 timers cancelled (no future snapshot
   events fire).
2. Second `aclose()` is idempotent (no exception).
3. After `aclose()`, `impl.open("t")` raises `RuntimeError`.
4. Drain task is cancelled (task's `.done()` returns True after
   aclose completes).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from cli.chat.services.cards_impl import CardsImpl
from schemas.events import EventType
from schemas.wire_kinds import WIRE_CARD_SNAPSHOT
from store import SessionStore, SqliteBackend

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


async def _mk_impl(store: SessionStore) -> CardsImpl:
    session = await store.create_session("ag_test", name="c")
    threads = await store.list_threads(session.session_id)
    return CardsImpl(
        session_store=store,
        session_id=session.session_id,
        thread_id=threads[0].thread_id,
    )


async def _count_snapshots(store: SessionStore, thread_id: str) -> int:
    n = 0
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == WIRE_CARD_SNAPSHOT:
            n += 1
    return n


async def test_aclose_cancels_all_pending_snapshot_timers(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    # Short timer so we can prove it doesn't fire post-aclose.
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "1000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "80")
    impl = await _mk_impl(store)

    cards = [await impl.open(f"t{i}", kind="markdown") for i in range(3)]
    for card in cards:
        await card.append("chunk")

    # Every card has a scheduled snapshot timer.
    assert all(c._snapshot_timer is not None for c in cards)

    await impl.aclose()

    # All timers cancelled — timer handle is None (per _cancel_snapshot_timer).
    assert all(c._snapshot_timer is None for c in cards)

    # Wait past the original fire time; confirm no snapshot lands in the store.
    await asyncio.sleep(0.200)
    n_snap = await _count_snapshots(store, impl._thread_id)
    assert n_snap == 0


async def test_aclose_second_call_idempotent(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    await impl.aclose()
    # Second call must not raise.
    await impl.aclose()


async def test_open_after_aclose_raises_runtime_error(
    store: SessionStore,
) -> None:
    impl = await _mk_impl(store)
    await impl.aclose()
    with pytest.raises(RuntimeError):
        await impl.open("t", kind="markdown")


async def test_drain_task_cancelled_after_aclose(store: SessionStore) -> None:
    impl = await _mk_impl(store)
    # Trigger drain-task start.
    await impl.open("t", kind="markdown")
    assert impl._drain_task is not None
    await impl.aclose()
    assert impl._drain_task is not None
    assert impl._drain_task.done()
