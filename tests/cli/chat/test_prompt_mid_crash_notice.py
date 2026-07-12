"""spec-008 Test 7 — Boot-time interruption notice.

Two passes:

1. Session with a dangling prompt. Boot the ``EventAdapter``;
   ``_scan_dangling_prompts`` synthesises ``tool.failed`` (from
   spec-003). ``Cards.notice("↻ Previous session was interrupted…",
   kind="warning")`` fires at transcript root.
2. Session with no dangling prompts. Boot. No notice fires.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from schemas import Actor, Event, EventType, SessionMeta
from schemas.identifiers import EventId
from schemas.wire_kinds import (
    WIRE_PROMPT_ANSWERED,
    WIRE_PROMPT_REQUESTED,
)
from store import SessionStore, SqliteBackend


class _MockChatApp:
    """Minimal stand-in — the adapter's card/notice mount paths are
    stubbed by ``_locate_transcript`` returning None in headless
    mode; the notice fires through the ``Cards`` seam we track here.
    """

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


class _RecordingCards:
    """Cards double that records ``notice`` calls for the test."""

    def __init__(self) -> None:
        self.notices: list[dict[str, Any]] = []

    def notice(
        self,
        text: str,
        *,
        kind: str = "info",
        card: Any = None,
    ) -> None:
        self.notices.append(
            {
                "text": text,
                "kind": kind,
                "card_id": (
                    str(card.id) if card is not None else None
                ),
            },
        )


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
) -> None:
    await store.append_event(
        thread_id,
        _mk_event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.TOOL_CALLED,
            data={"tool_call_id": tool_call_id, "tool_name": "read_file", "arguments": {}},
        ),
    )


async def _append_prompt_requested(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
    tool_call_id: str,
) -> None:
    await store.append_event(
        thread_id,
        _mk_event(
            session_id=session_id,
            thread_id=thread_id,
            event_type=EventType.UI_EVENT,
            data={
                "schema_version": 1,
                "kind": WIRE_PROMPT_REQUESTED,
                "payload": {
                    "prompt_id": prompt_id,
                    "prompt": "?",
                    "options": ["a"],
                    "source": {
                        "kind": "tool",
                        "id": tool_call_id,
                        "tool_call_id": tool_call_id,
                    },
                },
            },
        ),
    )


async def _append_prompt_answered(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    prompt_id: str,
) -> None:
    await store.append_event(
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


def _make_adapter(
    store: SessionStore,
    session_id: Any,
    thread_id: Any,
    cards: _RecordingCards | None,
) -> EventAdapter:
    return EventAdapter(
        _MockChatApp(),  # type: ignore[arg-type]
        session_store=store,
        session_id=session_id,
        thread_id=thread_id,
        session_meta=SessionMeta(),
        cards=cards,
    )


async def test_dangling_prompt_fires_interruption_notice(
    store: SessionStore,
) -> None:
    session = await store.create_session("ag_test", name="p1")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    tool_call_id = "call_abc"
    prompt_id = "prm_xyz"
    await _append_tool_called(store, session.session_id, thread_id, tool_call_id)
    await _append_prompt_requested(
        store, session.session_id, thread_id, prompt_id, tool_call_id,
    )

    cards = _RecordingCards()
    adapter = _make_adapter(store, session.session_id, thread_id, cards)
    await adapter.start()

    # One interruption notice fired at transcript root.
    warning = [n for n in cards.notices if n["kind"] == "warning"]
    assert len(warning) == 1
    assert "interrupted" in warning[0]["text"].lower()
    # Transcript root — no card_id.
    assert warning[0]["card_id"] is None


async def test_no_dangling_prompts_no_notice(
    store: SessionStore,
) -> None:
    session = await store.create_session("ag_test", name="p2")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    # A prompt that WAS answered — no dangling entries.
    tool_call_id = "call_abc"
    prompt_id = "prm_xyz"
    await _append_tool_called(store, session.session_id, thread_id, tool_call_id)
    await _append_prompt_requested(
        store, session.session_id, thread_id, prompt_id, tool_call_id,
    )
    await _append_prompt_answered(
        store, session.session_id, thread_id, prompt_id,
    )

    cards = _RecordingCards()
    adapter = _make_adapter(store, session.session_id, thread_id, cards)
    await adapter.start()

    # No notices fired — scan found no dangling prompts.
    assert cards.notices == []


async def test_no_cards_impl_wired_skips_notice(
    store: SessionStore,
) -> None:
    """Missing Cards (headless replay-only paths) — synthesis still
    happens but the notice is silently skipped.
    """
    session = await store.create_session("ag_test", name="p3")
    thread_id = (await store.list_threads(session.session_id))[0].thread_id

    tool_call_id = "call_abc"
    prompt_id = "prm_xyz"
    await _append_tool_called(store, session.session_id, thread_id, tool_call_id)
    await _append_prompt_requested(
        store, session.session_id, thread_id, prompt_id, tool_call_id,
    )

    adapter = _make_adapter(store, session.session_id, thread_id, cards=None)
    # Must not raise — Cards is None.
    await adapter.start()
    # Test passes if the boot notice hook doesn't crash on None cards.
