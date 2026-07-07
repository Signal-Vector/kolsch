"""spec-003 Test 1 — `logical_seq` monotonicity under concurrency.

Three passes per the spec:

1. Append 100 events across 4 threads (all going through the same
   sole-writer flock per invariant I8); assert `logical_seq` values
   are `[1, 2, ..., 100]` with no gaps or duplicates.
2. Fresh JSONL: first append's `logical_seq == 1`.
3. Legacy JSONL whose tail carries `logical_seq=42`: next append's
   `logical_seq == 43`.

Fail means: race in allocation, or off-by-one in the tail-read
helper.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import pytest

from schemas import (
    Actor,
    AgentId,
    Event,
    EventType,
    SessionId,
    ThreadId,
)
from schemas.identifiers import EventId
from store import SessionStore, SqliteBackend
from store.jsonl import append_event_sync, jsonl_path


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


def _make_event(
    *,
    session_id: SessionId,
    thread_id: ThreadId,
    marker: str = "",
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.CONFIG_SET,
        data={"key": "seq_test", "value": marker},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_logical_seq_monotonic_under_concurrency(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    """100 concurrent appends across 4 tasks land as `[1..100]` no gaps."""
    session = await store.create_session("ag_test", name="seq-test")
    threads = await store.list_threads(session.session_id)
    default_thread = threads[0]
    thread_id = default_thread.thread_id

    # 100 events, four "concurrent" appenders. The `flock` in
    # `append_event_sync` serialises them; the goal is to prove no
    # race in seq allocation even when four tasks contend for the
    # lock. Note the session.opened event already has seq=1, so our
    # new events will be seqs 2..101.
    n = 100

    async def one_task(start: int, end: int) -> None:
        for i in range(start, end):
            await store.append_event(
                thread_id,
                _make_event(
                    session_id=session.session_id,
                    thread_id=thread_id,
                    marker=f"n{i}",
                ),
            )

    # 4 tasks, 25 events each.
    tasks = [
        asyncio.create_task(one_task(0, 25)),
        asyncio.create_task(one_task(25, 50)),
        asyncio.create_task(one_task(50, 75)),
        asyncio.create_task(one_task(75, 100)),
    ]
    await asyncio.gather(*tasks)

    # Read back every event's logical_seq from disk.
    events: list[Event] = []
    async for ev in store.read_events(thread_id):
        events.append(ev)

    seqs = [ev.data.get("logical_seq", 0) for ev in events]
    # session.opened + 100 appends = 101 total.
    assert len(seqs) == n + 1, f"expected {n + 1} events, got {len(seqs)}"
    assert seqs == list(range(1, n + 2)), (
        f"logical_seq is not strictly monotonic: {seqs[:10]}...{seqs[-5:]}"
    )


@pytest.mark.asyncio
async def test_logical_seq_starts_at_one_on_fresh_jsonl(
    store: SessionStore,
) -> None:
    """First append to a fresh session (empty tail) gets `logical_seq == 1`."""
    session = await store.create_session("ag_test", name="seq-fresh")
    threads = await store.list_threads(session.session_id)
    default_thread = threads[0]

    # `session.opened` was written by create_session and should carry
    # logical_seq=1.
    events: list[Event] = []
    async for ev in store.read_events(default_thread.thread_id):
        events.append(ev)

    assert len(events) == 1
    assert events[0].data.get("logical_seq") == 1


@pytest.mark.asyncio
async def test_logical_seq_increments_from_existing_tail(
    tmp_path: Path,
) -> None:
    """A JSONL whose tail has `logical_seq=42` produces `43` on next append."""
    # Craft a legacy-shape JSONL by writing directly (bypass
    # SessionStore, which would allocate seq=1 first).
    session_id = SessionId(f"ses_{uuid4()}")
    thread_id = ThreadId(f"th_{uuid4()}")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    path = jsonl_path(tmp_path, session_id)

    # Write one line with logical_seq=42 on the data payload.
    legacy_event = Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.CONFIG_SET,
        data={"key": "existing", "value": "tail", "logical_seq": 42},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )
    # Serialize verbatim (bypass the strip so 42 is preserved).
    payload = legacy_event.model_dump(mode="json")
    if payload.get("trace_id") is None:
        payload.pop("trace_id", None)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    path.write_text(line, encoding="utf-8")

    # Now append via the sync primitive.
    new_event = Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.CONFIG_SET,
        data={"key": "new", "value": "append"},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )
    appended = append_event_sync(
        root=tmp_path,
        session_id=session_id,
        thread_id=thread_id,
        event=new_event,
    )
    assert appended.data.get("logical_seq") == 43
