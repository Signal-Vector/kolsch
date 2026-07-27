"""qol-100 — `SessionStore.has_any_event_of_type` presence probe.

Cheap short-circuiting presence-check that stops parsing on the first
matching event. Backs the eager-compaction path's "does this thread
have real user history?" gate without the full-log read
``read_path_to_leaf`` + ``any(...)`` paid post-bug-098.

Fixtures are minimal — reuses the store fixtures from
``tests/component/test_store.py`` via shared imports would couple the
files awkwardly; instead this file mints its own tiny scaffold so a
future refactor of the shared fixtures doesn't cascade here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from schemas import (
    Actor,
    Event,
    EventId,
    EventType,
    SessionId,
    ThreadId,
)
from store import SessionStore, SqliteBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def _event(
    *,
    session_id: SessionId,
    thread_id: ThreadId,
    event_type: EventType,
    actor_kind: str = "user",
    data: dict[str, Any] | None = None,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4().hex}"),
        parent_event_id=None,
        session_id=session_id,
        thread_id=thread_id,
        created_at=datetime.now(UTC),
        type=event_type,
        data=data or {},
        actor=Actor(kind=actor_kind, id=None),
    )


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    (tmp_path / ".platform").mkdir()
    backend = SqliteBackend(tmp_path / ".platform" / "entities.db")
    await backend.initialize()
    try:
        yield SessionStore(root=tmp_path, backend=backend)
    finally:
        await backend.close()


@pytest.fixture
async def session_thread(store: SessionStore) -> tuple[SessionId, ThreadId]:
    session = await store.create_session("ag_test")
    threads = await store.list_threads(session.session_id)
    return session.session_id, threads[0].thread_id


@pytest.mark.asyncio
async def test_has_any_event_of_type_true_when_present(
    store: SessionStore,
    session_thread: tuple[SessionId, ThreadId],
) -> None:
    """qol-100 AC: returns True on the first matching event."""
    session_id, thread_id = session_thread
    await store.append_event(
        thread_id,
        _event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.USER_MESSAGE,
            data={"text": "hi"},
        ),
    )

    assert await store.has_any_event_of_type(thread_id, EventType.USER_MESSAGE)


@pytest.mark.asyncio
async def test_has_any_event_of_type_false_when_absent(
    store: SessionStore,
    session_thread: tuple[SessionId, ThreadId],
) -> None:
    """qol-100: returns False when no event of the requested type exists.

    ``create_session`` bootstraps a ``SESSION_OPENED`` event, so the
    thread is non-empty — this test proves the probe filters by type
    and doesn't just return "does the log have anything".
    """
    _, thread_id = session_thread
    assert not await store.has_any_event_of_type(thread_id, EventType.USER_MESSAGE)


@pytest.mark.asyncio
async def test_has_any_event_of_type_short_circuits_on_first_match(
    store: SessionStore,
    session_thread: tuple[SessionId, ThreadId],
) -> None:
    """qol-100 AC: the probe stops reading after the first match.

    Instruments the underlying ``read_lines`` iterator with a counter
    and verifies that parsing stops as soon as a match is found. If
    the probe materialised the full list, all N events would be
    parsed regardless of position.
    """
    session_id, thread_id = session_thread
    # Append 5 events; the USER_MESSAGE is at position 2 (index 2 of
    # SESSION_OPENED + AGENT_MESSAGE + USER_MESSAGE + AGENT_MESSAGE +
    # AGENT_MESSAGE — SESSION_OPENED already appended by bootstrap).
    await store.append_event(
        thread_id,
        _event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.AGENT_MESSAGE,
            actor_kind="agent",
        ),
    )
    await store.append_event(
        thread_id,
        _event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.USER_MESSAGE,
        ),
    )
    for _ in range(2):
        await store.append_event(
            thread_id,
            _event(
                session_id=session_id,
                thread_id=thread_id,
                event_type=EventType.AGENT_MESSAGE,
                actor_kind="agent",
            ),
        )

    # Spy on read_lines to count how many events get parsed.
    import store.jsonl as jsonl_module

    original_read_lines = jsonl_module.read_lines
    parsed_count = 0

    def _counting_read_lines(path: Any) -> Any:
        nonlocal parsed_count
        for event in original_read_lines(path):
            parsed_count += 1
            yield event

    # Monkeypatch the reference the session_store module resolved at
    # import time (from store.jsonl import read_lines).
    import store.session_store as ss_module

    original_ref = ss_module.read_lines
    ss_module.read_lines = _counting_read_lines
    try:
        result = await store.has_any_event_of_type(thread_id, EventType.USER_MESSAGE)
    finally:
        ss_module.read_lines = original_ref

    assert result is True
    # We appended 4 events (agent, user, agent, agent) after
    # SESSION_OPENED. The USER_MESSAGE lands at raw position 3
    # (0-indexed). The probe should stop AT that event, so
    # parsed_count should be 3 (1-indexed count of events seen up to
    # and including the match). Assert <= 3 to allow for the
    # bootstrap event count changing.
    assert parsed_count <= 3, (
        f"probe read {parsed_count} events but the USER_MESSAGE was at "
        f"position 3; it should have short-circuited"
    )
