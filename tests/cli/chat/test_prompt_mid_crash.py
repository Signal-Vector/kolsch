"""spec-003 Test 3 — prompt-mid-crash synthesis on boot.

Five passes per the spec:

1. Session with a dangling `kolsch.prompt.requested` (no matching
   `.answered` or `.cancelled`) preceded by matching `tool.called`
   with no `tool.result`. `EventAdapter.start()` synthesises the
   `.cancelled` + `tool.failed`.
2. Second boot on the same session: no new synthetics
   (`_scan_dangling_prompts` is idempotent because the newly
   appended `.cancelled` resolves the request).
3. Session with a resolved prompt (has `.answered`): no synthetics.
4. Session with 1500 events + a dangling prompt in the FIRST 500:
   scan bound 1000 → the dangling prompt is OUT of the window and
   is NOT synthesised. Documented behaviour.
5. Session with a dangling prompt whose parent `tool.called` DID
   receive a `tool.result` later: only `.cancelled` fires, no
   `tool.failed`.
"""

from __future__ import annotations

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
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_CANCELLED,
    WIRE_PROMPT_REQUESTED,
)
from store import SessionStore, SqliteBackend


class _MockChatApp:
    def __init__(self) -> None:
        self._last_assistant: str = ""

    async def mount_user_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_assistant_message(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_assistant_streaming_widget(self) -> Any:
        class _MockProse:
            async def append(self, text: str) -> None:
                return None

        return _MockProse()

    async def mount_tool_call(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_notice(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mount_exchange_failed(self, *args: Any, **kwargs: Any) -> None:
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


def _mk_event(
    *,
    session_id: Any,
    thread_id: Any,
    event_type: EventType,
    data: dict[str, Any],
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=thread_id,
        session_id=session_id,
        type=event_type,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


async def _append_tool_called(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    tool_call_id: str,
    tool_name: str = "read_file",
) -> Event:
    return await store.append_event(
        thread_id,
        _mk_event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.TOOL_CALLED,
            data={"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": {}},
        ),
    )


async def _append_prompt_requested(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
    tool_call_id: str | None = None,
) -> Event:
    payload: dict[str, Any] = {"prompt_id": prompt_id, "prompt": "?", "options": ["a"]}
    if tool_call_id is not None:
        payload["source"] = {"kind": "tool", "id": tool_call_id, "tool_call_id": tool_call_id}
    return await store.append_event(
        thread_id,
        _mk_event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": WIRE_PROMPT_REQUESTED,
                "payload": payload,
            },
        ),
    )


async def _append_prompt_answered(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
) -> Event:
    return await store.append_event(
        thread_id,
        _mk_event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": WIRE_PROMPT_ANSWERED,
                "payload": {"prompt_id": prompt_id, "answer": "a"},
            },
        ),
    )


async def _append_tool_result(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    tool_call_id: str,
) -> Event:
    return await store.append_event(
        thread_id,
        _mk_event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.TOOL_RESULT,
            data={
                "tool_call_id": tool_call_id,
                "tool_name": "read_file",
                "result": {"content": "ok"},
            },
        ),
    )


def _make_adapter(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
) -> EventAdapter:
    app = _MockChatApp()
    return EventAdapter(
        app,  # type: ignore[arg-type]
        session_store=store,
        session_id=session_id,
        thread_id=thread_id,
        session_meta=SessionMeta(),
    )


async def _collect(store: SessionStore, thread_id: Any) -> list[Event]:
    events: list[Event] = []
    async for ev in store.read_events(thread_id):
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_dangling_prompt_synthesises_cancelled_and_tool_failed(
    store: SessionStore,
) -> None:
    session = await store.create_session("ag_test", name="p1")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    tool_call_id = "call_abc"
    prompt_id = "prm_xyz"
    await _append_tool_called(store, session.session_id, thread_id, tool_call_id)
    await _append_prompt_requested(
        store,
        session.session_id,
        thread_id,
        prompt_id,
        tool_call_id=tool_call_id,
    )

    adapter = _make_adapter(store, session.session_id, thread_id)
    await adapter.start()

    events = await _collect(store, thread_id)
    kinds = [
        (ev.data.get("kind"), ev.data.get("payload", {}).get("prompt_id"))
        for ev in events
        if ev.type == EventType.UI_EVENT
    ]
    # One requested, one synthetic cancelled.
    assert (WIRE_PROMPT_REQUESTED, prompt_id) in kinds
    cancelled = [
        ev for ev in events
        if ev.type == EventType.UI_EVENT
        and ev.data.get("kind") == WIRE_PROMPT_CANCELLED
    ]
    assert len(cancelled) == 1
    assert cancelled[0].data["payload"]["reason"] == "session_interrupted"
    assert cancelled[0].data["payload"]["prompt_id"] == prompt_id

    # tool.failed synthesised too.
    failed = [ev for ev in events if ev.type == EventType.TOOL_FAILED]
    assert len(failed) == 1
    assert failed[0].data["tool_call_id"] == tool_call_id
    assert failed[0].data["error"]["type"] == "session.interrupted"


@pytest.mark.asyncio
async def test_second_boot_is_idempotent(store: SessionStore) -> None:
    session = await store.create_session("ag_test", name="p2")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    tool_call_id = "call_abc"
    prompt_id = "prm_xyz"
    await _append_tool_called(store, session.session_id, thread_id, tool_call_id)
    await _append_prompt_requested(
        store,
        session.session_id,
        thread_id,
        prompt_id,
        tool_call_id=tool_call_id,
    )

    adapter_1 = _make_adapter(store, session.session_id, thread_id)
    await adapter_1.start()
    events_after_first = await _collect(store, thread_id)

    # Second boot: fresh adapter, bookmark advanced past the
    # synthetics.
    adapter_2 = _make_adapter(store, session.session_id, thread_id)
    await adapter_2.start()
    events_after_second = await _collect(store, thread_id)

    # No new synthetics.
    assert len(events_after_second) == len(events_after_first)


@pytest.mark.asyncio
async def test_resolved_prompt_no_synthetics(store: SessionStore) -> None:
    session = await store.create_session("ag_test", name="p3")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    tool_call_id = "call_abc"
    prompt_id = "prm_xyz"
    await _append_tool_called(store, session.session_id, thread_id, tool_call_id)
    await _append_prompt_requested(
        store,
        session.session_id,
        thread_id,
        prompt_id,
        tool_call_id=tool_call_id,
    )
    # Resolved by an `.answered` event.
    await _append_prompt_answered(store, session.session_id, thread_id, prompt_id)

    before = await _collect(store, thread_id)
    adapter = _make_adapter(store, session.session_id, thread_id)
    await adapter.start()
    after = await _collect(store, thread_id)

    # No new events.
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_scan_bounded_to_last_1000(store: SessionStore) -> None:
    """Dangling prompt in the FIRST 500 of 1500 → out of scan window."""
    session = await store.create_session("ag_test", name="p4")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    tool_call_id = "call_early"
    prompt_id = "prm_early"
    # Put the dangling prompt at the START of the session.
    await _append_tool_called(store, session.session_id, thread_id, tool_call_id)
    await _append_prompt_requested(
        store,
        session.session_id,
        thread_id,
        prompt_id,
        tool_call_id=tool_call_id,
    )
    # Fill with lots of unrelated events so the dangling is > 1000
    # back from the tail. 1497 more events → total ~1500.
    # Note session.opened is event 1; tool.called is 2; requested is 3.
    for i in range(1497):
        await store.append_event(
            thread_id,
            _mk_event(
                session_id=session.session_id,
                thread_id=thread_id,
                event_type=EventType.CONFIG_SET,
                data={"key": f"pad_{i}", "value": str(i)},
            ),
        )

    before_count = len(await _collect(store, thread_id))
    adapter = _make_adapter(store, session.session_id, thread_id)
    await adapter.start()
    after_events = await _collect(store, thread_id)

    # No synthetics: the dangling prompt lives outside the last-1000 window.
    assert len(after_events) == before_count
    cancelled = [
        ev for ev in after_events
        if ev.type == EventType.UI_EVENT
        and ev.data.get("kind") == WIRE_PROMPT_CANCELLED
    ]
    assert cancelled == []


@pytest.mark.asyncio
async def test_dangling_prompt_with_resolved_tool_only_cancelled(
    store: SessionStore,
) -> None:
    """Parent tool.called received a tool.result → only synthesise .cancelled."""
    session = await store.create_session("ag_test", name="p5")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    tool_call_id = "call_abc"
    prompt_id = "prm_xyz"
    await _append_tool_called(store, session.session_id, thread_id, tool_call_id)
    await _append_prompt_requested(
        store,
        session.session_id,
        thread_id,
        prompt_id,
        tool_call_id=tool_call_id,
    )
    # Rare race: tool.result arrived (parent already resolved) but the
    # prompt was never answered/cancelled.
    await _append_tool_result(store, session.session_id, thread_id, tool_call_id)

    before = await _collect(store, thread_id)
    tool_failed_before = sum(1 for ev in before if ev.type == EventType.TOOL_FAILED)

    adapter = _make_adapter(store, session.session_id, thread_id)
    await adapter.start()
    after = await _collect(store, thread_id)

    cancelled = [
        ev for ev in after
        if ev.type == EventType.UI_EVENT
        and ev.data.get("kind") == WIRE_PROMPT_CANCELLED
    ]
    assert len(cancelled) == 1

    tool_failed_after = sum(1 for ev in after if ev.type == EventType.TOOL_FAILED)
    # No synthetic tool.failed.
    assert tool_failed_after == tool_failed_before
