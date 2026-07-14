"""spec-003 Test 6 — legacy events (missing `logical_seq`) round-trip.

Two passes per the spec:

1. Craft a JSONL with events written before spec-002 (no
   `logical_seq` in the data payload). Read back via
   `SessionStore.read_events`; assert each event's
   `data.logical_seq == 0` (default).
2. Append a new event to the legacy JSONL; assert its
   `logical_seq == 1` (empty tail: last found seq is 0, new is 0+1).

Fail means: legacy events fail to validate, or the tail-read helper
crashes on missing fields.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

import pytest

from schemas import (
    Actor,
    Event,
    EventType,
    SessionId,
    ThreadId,
    Thread,
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


def _legacy_line(event: Event) -> str:
    """Serialise `event` in the pre-spec-002 shape — no `logical_seq`."""
    payload = event.model_dump(mode="json")
    if payload.get("trace_id") is None:
        payload.pop("trace_id", None)
    data = payload.get("data")
    if isinstance(data, dict):
        # spec-002 didn't strip; legacy events lack the field entirely.
        data.pop("logical_seq", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _make_event(
    *,
    session_id: SessionId,
    thread_id: ThreadId,
    marker: str,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=EventType.CONFIG_SET,
        data={"key": "legacy", "value": marker},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_legacy_events_read_with_default_logical_seq_zero(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    """Legacy events (no `logical_seq` in JSON) validate with default 0."""
    # Mint a session so backend records exist; overwrite the JSONL by
    # hand with the legacy shape.
    session = await store.create_session("ag_test", name="legacy")
    threads = await store.list_threads(session.session_id)
    default_thread = threads[0]
    path = jsonl_path(tmp_path, session.session_id)

    # Overwrite the JSONL with two legacy-shape events (no
    # logical_seq). Note: the session.opened event created by
    # create_session lives here; we replace it.
    ev1 = _make_event(
        session_id=session.session_id,
        thread_id=default_thread.thread_id,
        marker="a",
    )
    ev2 = _make_event(
        session_id=session.session_id,
        thread_id=default_thread.thread_id,
        marker="b",
    )
    path.write_text(_legacy_line(ev1) + _legacy_line(ev2), encoding="utf-8")

    # Read back — each event validates with default logical_seq=0.
    events: list[Event] = []
    async for ev in store.read_events(default_thread.thread_id):
        events.append(ev)
    assert len(events) == 2
    for ev in events:
        assert ev.data.get("logical_seq", 0) == 0


@pytest.mark.asyncio
async def test_append_to_legacy_jsonl_starts_at_one(
    tmp_path: Path,
) -> None:
    """Append to a legacy JSONL: first new event's seq is 1 (tail seq treated as 0)."""
    session_id = SessionId(f"ses_{uuid4()}")
    thread_id = ThreadId(f"th_{uuid4()}")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    path = jsonl_path(tmp_path, session_id)

    # Write two legacy events (no logical_seq).
    ev1 = _make_event(session_id=session_id, thread_id=thread_id, marker="a")
    ev2 = _make_event(session_id=session_id, thread_id=thread_id, marker="b")
    path.write_text(_legacy_line(ev1) + _legacy_line(ev2), encoding="utf-8")

    # Append a new event via the sync primitive.
    new = _make_event(session_id=session_id, thread_id=thread_id, marker="new")
    appended = append_event_sync(
        root=tmp_path,
        session_id=session_id,
        thread_id=thread_id,
        event=new,
    )
    # Tail was 0 (legacy events without the field), new is 0+1=1.
    assert appended.data.get("logical_seq") == 1
