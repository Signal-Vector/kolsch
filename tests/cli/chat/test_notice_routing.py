"""spec-008 Test 4 — ``EventAdapter._handle_card_notice`` routing.

Four passes:

1. Notice with no ``card_id`` mounts at transcript root as
   ``NoticeWidget``.
2. Notice with ``card_id`` matching a mounted card mounts INSIDE
   that card's ``NoticeSlot``.
3. Notice with ``card_id`` not in ``_card_widgets`` — dropped
   silently.
4. Second notice with same ``notice_id`` updates the existing
   widget's text (drop-notice count increment); no second widget
   mounts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.event_adapter import EventAdapter
from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.notice import NoticeWidget
from cli.chat.widgets.notice_slot import NoticeSlot
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import CardId, EventId, SessionId, ThreadId
from schemas.wire_kinds import WIRE_CARD_NOTICE, WIRE_CARD_OPENED


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")


def _mk_ui_event(kind: str, payload: dict[str, Any]) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.UI_EVENT,
        data={"schema_version": 1, "kind": kind, "payload": payload},
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_notice_without_card_id_mounts_at_transcript_root() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)  # type: ignore[arg-type]
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_NOTICE,
                {
                    "notice_id": f"ntc_{uuid4()}",
                    "text": "hi",
                    "kind": "info",
                },
            ),
        )
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        widgets = list(transcript.query(NoticeWidget))
        # No `card_id` → mounted at transcript root.
        assert len(widgets) == 1
        assert widgets[0].text == "hi"


@pytest.mark.asyncio
async def test_notice_with_card_id_mounts_inside_card() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)  # type: ignore[arg-type]
        card_id = f"crd_{uuid4()}"
        # First mount the card.
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_OPENED,
                {"card_id": card_id, "title": "t", "kind": "markdown"},
            ),
        )
        await pilot.pause()

        # Notice targeted at that card.
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_NOTICE,
                {
                    "notice_id": f"ntc_{uuid4()}",
                    "text": "in-card",
                    "kind": "info",
                    "card_id": card_id,
                },
            ),
        )
        await pilot.pause()

        # The NoticeWidget lives inside the CardWidget's NoticeSlot.
        card_widget = app.query_one(CardWidget)
        slot = card_widget.query_one(NoticeSlot)
        widgets = list(slot.query(NoticeWidget))
        assert len(widgets) == 1
        assert widgets[0].text == "in-card"

        # No transcript-root NoticeWidget for this notice.
        transcript = app.query_one("#transcript", VerticalScroll)
        root_widgets = [
            w for w in transcript.query(NoticeWidget)
            if w.parent is transcript
        ]
        assert root_widgets == []


@pytest.mark.asyncio
async def test_notice_for_unknown_card_id_dropped_silently() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)  # type: ignore[arg-type]
        # No card mounted yet — send a notice for one anyway.
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_NOTICE,
                {
                    "notice_id": f"ntc_{uuid4()}",
                    "text": "orphan",
                    "kind": "info",
                    "card_id": f"crd_{uuid4()}",
                },
            ),
        )
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        widgets = list(transcript.query(NoticeWidget))
        # Silently dropped — no widget mounted anywhere.
        assert widgets == []


@pytest.mark.asyncio
async def test_repeat_notice_id_updates_existing_widget_in_place() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)  # type: ignore[arg-type]
        card_id = f"crd_{uuid4()}"
        notice_id = f"ntc_{uuid4()}"
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_OPENED,
                {"card_id": card_id, "title": "t", "kind": "markdown"},
            ),
        )
        await pilot.pause()

        # First notice.
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_NOTICE,
                {
                    "notice_id": notice_id,
                    "text": "⚠ dropped 10 events",
                    "kind": "warning",
                    "card_id": card_id,
                },
            ),
        )
        await pilot.pause()

        # Second notice with same notice_id — count increment.
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_NOTICE,
                {
                    "notice_id": notice_id,
                    "text": "⚠ dropped 20 events",
                    "kind": "warning",
                    "card_id": card_id,
                },
            ),
        )
        await pilot.pause()

        card_widget = app.query_one(CardWidget)
        slot = card_widget.query_one(NoticeSlot)
        widgets = list(slot.query(NoticeWidget))
        # Only ONE widget in the slot — updated in place.
        assert len(widgets) == 1
        assert widgets[0].text == "⚠ dropped 20 events"
