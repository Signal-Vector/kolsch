"""spec-006 Test 4 — Snapshot coalescing timing.

Five passes:

1. `KOLSCH_CARD_SNAPSHOT_N=3`; append 10 chunks. Assert 3
   `WIRE_CARD_SNAPSHOT` events fire (at chunks 3, 6, 9) plus one on
   close (chunk 10 lands after the third and is un-superseded).
2. `KOLSCH_CARD_SNAPSHOT_MS=100`; append 1 chunk, sleep 150ms,
   append 1. Assert >=1 snapshot fires from timer path.
3. Close card after 5 appends since last snapshot; assert final
   snapshot fires with `content` equal to their concatenation.
4. Snapshot event payload includes `superseded_event_ids` list of
   the `event_id`s the snapshot supersedes.
5. Snapshot timer cancelled on close: schedule at T=100ms, close at
   T=50ms; sleep 200ms; assert NO post-close snapshot fires.
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


async def _snapshot_payloads(
    store: SessionStore,
    thread_id: str,
) -> list[dict]:
    out: list[dict] = []
    async for ev in store.read_events(thread_id):
        if ev.type != EventType.UI_EVENT:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        if data.get("kind") == WIRE_CARD_SNAPSHOT:
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                out.append(payload)
    return out


async def test_n_appends_triggers_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "3")
    # Long MS so the timer path doesn't fire during this test.
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "60000")
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        for i in range(10):
            await card.append(f"c{i}")

        snapshots = await _snapshot_payloads(store, impl._thread_id)
        # Snapshots fire at count 3, 6, 9 → 3 during appends.
        assert len(snapshots) == 3

        await card.close()
        # Final snapshot on close for the 10th append (un-superseded).
        snapshots_after = await _snapshot_payloads(store, impl._thread_id)
        assert len(snapshots_after) == 4
    finally:
        await impl.aclose()


async def test_ms_timer_triggers_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    # Large N so only the timer path can fire.
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "1000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "100")
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        await card.append("first")
        # Let the timer fire.
        await asyncio.sleep(0.25)
        await card.append("second")

        snapshots = await _snapshot_payloads(store, impl._thread_id)
        assert len(snapshots) >= 1
        # First snapshot content should be the first chunk.
        assert snapshots[0]["content"] == "first"
    finally:
        await impl.aclose()


async def test_close_flushes_final_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    # Large N + MS so no snapshot fires until close.
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "1000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "60000")
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        chunks = ["a", "b", "c", "d", "e"]
        for chunk in chunks:
            await card.append(chunk)
        await card.close()

        snapshots = await _snapshot_payloads(store, impl._thread_id)
        assert len(snapshots) == 1
        assert snapshots[0]["content"] == "abcde"
    finally:
        await impl.aclose()


async def test_snapshot_carries_superseded_event_ids(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "3")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "60000")
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        for i in range(3):
            await card.append(f"c{i}")

        snapshots = await _snapshot_payloads(store, impl._thread_id)
        assert len(snapshots) == 1
        ids = snapshots[0].get("superseded_event_ids")
        assert isinstance(ids, list)
        # Three appends → three superseded ids.
        assert len(ids) == 3
        assert all(isinstance(x, str) and x.startswith("ev_") for x in ids)
    finally:
        await impl.aclose()


async def test_snapshot_timer_cancelled_on_close(
    monkeypatch: pytest.MonkeyPatch,
    store: SessionStore,
) -> None:
    # Timer at 100ms; we close before it can fire and sleep past 100ms.
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_N", "1000")
    monkeypatch.setenv("KOLSCH_CARD_SNAPSHOT_MS", "100")
    impl = await _mk_impl(store)
    try:
        card = await impl.open("t", kind="markdown")
        await card.append("one")
        # Close before the timer fires.
        await asyncio.sleep(0.020)
        await card.close()
        snapshots_before = await _snapshot_payloads(store, impl._thread_id)
        # Close flushes final snapshot (one un-superseded append).
        assert len(snapshots_before) == 1

        # Wait past the original timer fire time.
        await asyncio.sleep(0.200)

        # No new snapshot fired post-close.
        snapshots_after = await _snapshot_payloads(store, impl._thread_id)
        assert len(snapshots_after) == 1
    finally:
        await impl.aclose()
