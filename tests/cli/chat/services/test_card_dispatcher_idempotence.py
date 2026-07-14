"""spec-006 Test 6 — Dispatcher idempotence.

Six passes:

1. Dispatch same `WIRE_CARD_OPENED` twice; `_card_widgets` has
   exactly one entry.
2. Dispatch same `WIRE_CARD_APPENDED` twice; entry `.content`
   equal to a single application (not doubled).
3. Dispatch same `WIRE_CARD_SNAPSHOT` twice; state consistent.
4. Dispatch same `WIRE_CARD_CLOSED` twice; state transitions once.
5. Bounded LRU behaviour: 1001st unique append `event_id` on a
   single card evicts the first — re-dispatching #1 now applies
   (documented behaviour).
6. `WIRE_CARD_NOTICE` idempotence: same notice_id twice → single
   entry in `_applied_notice_ids`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cli.chat.event_adapter import EventAdapter
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import CardId, EventId, SessionId, ThreadId
from schemas.wire_kinds import (
    WIRE_CARD_APPENDED,
    WIRE_CARD_CLOSED,
    WIRE_CARD_NOTICE,
    WIRE_CARD_OPENED,
    WIRE_CARD_SNAPSHOT,
)


def _mk_event(
    kind: str,
    payload: dict[str, Any],
    *,
    event_id: str | None = None,
) -> Event:
    return Event(
        event_id=EventId(event_id or f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.UI_EVENT,
        data={"schema_version": 1, "kind": kind, "payload": payload},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


class _StubApp:
    """Minimal stand-in for `ChatApp` — the card handlers never touch it."""

    def __init__(self) -> None:
        pass


def _mk_adapter() -> EventAdapter:
    return EventAdapter(_StubApp())  # type: ignore[arg-type]


async def test_opened_dispatched_twice_mounts_once() -> None:
    adapter = _mk_adapter()
    card_id = CardId(f"crd_{uuid4()}")
    ev = _mk_event(
        WIRE_CARD_OPENED,
        {"card_id": str(card_id), "title": "t", "kind": "markdown"},
    )
    await adapter.handle(ev)
    await adapter.handle(ev)
    assert len(adapter._card_widgets) == 1
    assert card_id in adapter._card_widgets


async def test_appended_dispatched_twice_applies_once() -> None:
    adapter = _mk_adapter()
    card_id = CardId(f"crd_{uuid4()}")
    await adapter.handle(
        _mk_event(
            WIRE_CARD_OPENED,
            {"card_id": str(card_id), "title": "t", "kind": "markdown"},
        ),
    )
    append_ev = _mk_event(
        WIRE_CARD_APPENDED,
        {"card_id": str(card_id), "chunk": "hello"},
    )
    await adapter.handle(append_ev)
    await adapter.handle(append_ev)

    entry = adapter._card_widgets[card_id]
    # Content applied exactly once — not doubled.
    assert entry.content == "hello"


async def test_snapshot_dispatched_twice_applies_once() -> None:
    adapter = _mk_adapter()
    card_id = CardId(f"crd_{uuid4()}")
    await adapter.handle(
        _mk_event(
            WIRE_CARD_OPENED,
            {"card_id": str(card_id), "title": "t", "kind": "markdown"},
        ),
    )
    snap_ev = _mk_event(
        WIRE_CARD_SNAPSHOT,
        {"card_id": str(card_id), "content": "full-snapshot", "superseded_event_ids": []},
    )
    await adapter.handle(snap_ev)
    await adapter.handle(snap_ev)

    entry = adapter._card_widgets[card_id]
    assert entry.content == "full-snapshot"


async def test_closed_dispatched_twice_transitions_once() -> None:
    adapter = _mk_adapter()
    card_id = CardId(f"crd_{uuid4()}")
    await adapter.handle(
        _mk_event(
            WIRE_CARD_OPENED,
            {"card_id": str(card_id), "title": "t", "kind": "markdown"},
        ),
    )
    closed_ev = _mk_event(
        WIRE_CARD_CLOSED,
        {"card_id": str(card_id), "final_status": "success", "error_message": None},
    )
    await adapter.handle(closed_ev)
    entry = adapter._card_widgets[card_id]
    assert entry.state == "success"

    # Second close with a different final_status is a no-op — state
    # stays at the first terminal transition.
    later_ev = _mk_event(
        WIRE_CARD_CLOSED,
        {"card_id": str(card_id), "final_status": "error", "error_message": "boom"},
    )
    await adapter.handle(later_ev)
    assert entry.state == "success"


async def test_append_lru_evicts_after_1001() -> None:
    """1001st unique append `event_id` evicts the first — re-dispatch of
    #1 now applies. Documented behaviour per spec-006 AC #11.
    """
    adapter = _mk_adapter()
    card_id = CardId(f"crd_{uuid4()}")
    await adapter.handle(
        _mk_event(
            WIRE_CARD_OPENED,
            {"card_id": str(card_id), "title": "t", "kind": "markdown"},
        ),
    )
    # First append.
    first_id = f"ev_{uuid4()}"
    first_ev = _mk_event(
        WIRE_CARD_APPENDED,
        {"card_id": str(card_id), "chunk": "F"},
        event_id=first_id,
    )
    await adapter.handle(first_ev)

    # Fill the LRU (1000 slots) — 999 more unique appends.
    for _ in range(999):
        await adapter.handle(
            _mk_event(
                WIRE_CARD_APPENDED,
                {"card_id": str(card_id), "chunk": "x"},
            ),
        )
    # Now 1000 in the LRU; first_id still present.
    entry = adapter._card_widgets[card_id]
    assert first_id in entry.applied_appends

    # 1001st unique push evicts the oldest — which is first_id.
    await adapter.handle(
        _mk_event(
            WIRE_CARD_APPENDED,
            {"card_id": str(card_id), "chunk": "y"},
        ),
    )
    assert first_id not in entry.applied_appends

    # Re-dispatch first_id → NOW applies (previous eviction).
    content_before = entry.content
    await adapter.handle(first_ev)
    assert entry.content == content_before + "F"


async def test_notice_id_dedupes() -> None:
    adapter = _mk_adapter()
    notice_id = f"ntc_{uuid4()}"
    for _ in range(3):
        await adapter.handle(
            _mk_event(
                WIRE_CARD_NOTICE,
                {"notice_id": notice_id, "text": "hi", "kind": "info"},
            ),
        )
    # Only one entry in the LRU.
    assert len(adapter._applied_notice_ids) == 1
    assert notice_id in adapter._applied_notice_ids
