"""spec-003 Test 2 — `EventAdapter` bookmark round-trip.

Four passes per the spec:

1. Session with 5 events, no bookmark. `start()` dispatches all 5.
   Bookmark advances to event 5's `event_id`.
2. Session with 5 events, bookmark at event 3. `start()` dispatches
   events 4 and 5 only.
3. Bookmark points at a nonexistent event. `start()` logs a warning
   and replays the entire log.
4. Every 100 dispatched events, bookmark persists to meta.json.
   Verify by dispatching 205 events and asserting `meta.json` was
   updated twice (at 100 and 200) + once more on `aclose`.

Fail means: bookmark not read/written correctly, or replay loops.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from schemas import (
    Actor,
    Event,
    EventType,
    SessionMeta,
)
from schemas.identifiers import EventId
from store import SessionStore, SqliteBackend


class _FakeChatApp:
    """Minimal ChatApp double — record every dispatched event by id."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self._last_assistant: str = ""

    async def mount_user_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_assistant_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_assistant_streaming_widget(self) -> Any:
        class _FakeProse:
            async def append(self, text: str) -> None:
                return None

        return _FakeProse()

    async def mount_tool_call(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_notice(self, text: str) -> None:
        return None

    async def mount_exchange_failed(self, message: str) -> None:
        return None

    async def set_turn_verb(self, *args: Any, **kwargs: Any) -> None:
        return None

    def clear_awaiting_approval(self) -> None:
        return None

    def last_assistant_text(self) -> str:
        return self._last_assistant

    def set_last_assistant_text(self, text: str) -> None:
        self._last_assistant = text


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


async def _seed_events(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    n: int,
) -> list[Event]:
    """Append `n` canonical CONFIG_SET events; return them for id inspection."""
    events: list[Event] = []
    for i in range(n):
        ev = Event(
            event_id=EventId(f"ev_{uuid4()}"),
            parent_event_id=None,
            thread_id=thread_id,
            session_id=session_id,
            type=EventType.CONFIG_SET,
            data={"key": "seed", "value": f"n{i}"},
            actor=Actor(kind="system"),
            created_at=datetime.now(UTC),
        )
        appended = await store.append_event(thread_id, ev)
        events.append(appended)
    return events


class _RecordingAdapter(EventAdapter):
    """Adapter that records dispatched event ids on top of default behaviour."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.dispatched_ids: list[str] = []

    async def handle(self, event: Event) -> None:  # type: ignore[override]
        self.dispatched_ids.append(str(event.event_id))
        # Delegate to default dispatch to make sure the wire path
        # exercises the same code.
        await super().handle(event)


@pytest.mark.asyncio
async def test_no_bookmark_replays_all_events(store: SessionStore) -> None:
    session = await store.create_session("ag_test", name="bm-1")
    threads = await store.list_threads(session.session_id)
    thread_id = threads[0].thread_id
    # 5 events (the session.opened already exists → total 6).
    seeded = await _seed_events(store, session.session_id, thread_id, 5)

    app = _FakeChatApp()
    adapter = _RecordingAdapter(
        app,  # type: ignore[arg-type]
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        session_meta=SessionMeta(),  # no bookmark
    )
    await adapter.start()

    # 6 events dispatched (session.opened + 5 seeded).
    assert len(adapter.dispatched_ids) == 6
    # Bookmark advanced to the last event.
    assert str(adapter._bookmark) == str(seeded[-1].event_id)


@pytest.mark.asyncio
async def test_bookmark_replays_only_after_marker(store: SessionStore) -> None:
    session = await store.create_session("ag_test", name="bm-2")
    threads = await store.list_threads(session.session_id)
    thread_id = threads[0].thread_id
    seeded = await _seed_events(store, session.session_id, thread_id, 5)
    # Bookmark at seeded[2] (3rd of the 5 appended). All 6 events on
    # disk are: [session.opened, s0, s1, s2, s3, s4]. Bookmark at s2
    # means we replay [s3, s4] only.
    meta = SessionMeta(event_adapter_bookmark=seeded[2].event_id)

    app = _FakeChatApp()
    adapter = _RecordingAdapter(
        app,  # type: ignore[arg-type]
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        session_meta=meta,
    )
    await adapter.start()

    assert adapter.dispatched_ids == [
        str(seeded[3].event_id),
        str(seeded[4].event_id),
    ]
    assert str(adapter._bookmark) == str(seeded[-1].event_id)


@pytest.mark.asyncio
async def test_unknown_bookmark_falls_back_to_full_replay(
    store: SessionStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = await store.create_session("ag_test", name="bm-3")
    threads = await store.list_threads(session.session_id)
    thread_id = threads[0].thread_id
    await _seed_events(store, session.session_id, thread_id, 5)
    # Bookmark to a nonexistent id.
    bogus = EventId(f"ev_{uuid4()}")
    meta = SessionMeta(event_adapter_bookmark=bogus)

    app = _FakeChatApp()
    adapter = _RecordingAdapter(
        app,  # type: ignore[arg-type]
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        session_meta=meta,
    )
    import logging
    caplog.set_level(logging.WARNING)
    await adapter.start()

    # All 6 events replayed.
    assert len(adapter.dispatched_ids) == 6
    # Warning logged.
    assert any(
        "bookmark" in record.getMessage() and "replaying entire log" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_bookmark_persists_every_100_dispatches(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    session = await store.create_session("ag_test", name="bm-4")
    threads = await store.list_threads(session.session_id)
    thread_id = threads[0].thread_id
    # 205 additional events → total 206 dispatches (session.opened + 205).
    # Bookmark persist_every=100; persistences fire at dispatch 100 and 200,
    # plus one on aclose() = 3 total.
    await _seed_events(store, session.session_id, thread_id, 205)

    app = _FakeChatApp()
    adapter = _RecordingAdapter(
        app,  # type: ignore[arg-type]
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        session_meta=SessionMeta(),
    )

    # Instrument `update_meta` to count calls.
    write_count = 0
    original_update = store.update_meta

    async def counting_update(*args: Any, **kwargs: Any) -> Any:
        nonlocal write_count
        write_count += 1
        return await original_update(*args, **kwargs)

    # Patch the bound method via monkeypatching the instance attribute.
    store.update_meta = counting_update  # type: ignore[method-assign]
    try:
        await adapter.start()
        # After replay, 2 persistences fired (at 100 and 200).
        assert write_count == 2, (
            f"expected 2 persistences during replay of 206 events, got {write_count}"
        )
        await adapter.aclose()
        # One more on aclose.
        assert write_count == 3, (
            f"expected 3 persistences total (2 mid-replay + 1 aclose), got {write_count}"
        )
    finally:
        store.update_meta = original_update  # type: ignore[method-assign]

    # Confirm the persisted bookmark on disk.
    meta_final = await store.read_meta(session.session_id)
    assert meta_final.event_adapter_bookmark is not None


@pytest.mark.asyncio
async def test_start_is_idempotent(store: SessionStore) -> None:
    """Second `start()` call must be a no-op."""
    session = await store.create_session("ag_test", name="bm-idem")
    threads = await store.list_threads(session.session_id)
    thread_id = threads[0].thread_id
    await _seed_events(store, session.session_id, thread_id, 3)

    app = _FakeChatApp()
    adapter = _RecordingAdapter(
        app,  # type: ignore[arg-type]
        session_store=store,
        session_id=session.session_id,
        thread_id=thread_id,
        session_meta=SessionMeta(),
    )
    await adapter.start()
    first_count = len(adapter.dispatched_ids)
    await adapter.start()
    # No re-dispatch.
    assert len(adapter.dispatched_ids) == first_count
