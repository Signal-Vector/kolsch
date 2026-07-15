"""spec-007 Test 7 — `EventAdapter._handle_card_opened` mounts widget.

Three Pilot passes:

1. Boot a minimal host app with a `#transcript` container. Dispatch
   `WIRE_CARD_OPENED`. Assert a `CardWidget` mounts in `#transcript`.
2. Second `WIRE_CARD_OPENED` with SAME `card_id`: no second mount
   (idempotence from spec-006).
3. `card.appended`, `.set_progress`, etc. dispatch to the mounted
   widget's `apply_event` / Protocol methods.
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
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import CardId, EventId, SessionId, ThreadId
from schemas.wire_kinds import (
    WIRE_CARD_APPENDED,
    WIRE_CARD_OPENED,
    WIRE_CARD_SET_PROGRESS,
)


class _HostApp(App):
    """Minimal host app carrying a `#transcript` container.

    The EventAdapter looks up `#transcript` via `query_one` to mount
    the `CardWidget`. Keeping the host tiny lets the tests focus on
    the adapter's mount contract without booting the full ChatApp.
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")


def _mk_opened(card_id: str, kind: str = "markdown", title: str = "t") -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.UI_EVENT,
        data={
            "schema_version": 1,
            "kind": WIRE_CARD_OPENED,
            "payload": {"card_id": card_id, "kind": kind, "title": title},
        },
        actor=Actor(kind="system"),
        created_at=datetime.now(UTC),
    )


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
async def test_opened_mounts_card_widget_in_transcript() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)  # type: ignore[arg-type]
        card_id = f"crd_{uuid4()}"
        await adapter.handle(_mk_opened(card_id, kind="markdown", title="hello"))
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        widgets = list(transcript.query(CardWidget))
        assert len(widgets) == 1
        assert widgets[0].kind == "markdown"

        # `_card_widgets` dict holds the CardWidget (Protocol widening).
        assert CardId(card_id) in adapter._card_widgets
        assert isinstance(adapter._card_widgets[CardId(card_id)], CardWidget)


@pytest.mark.asyncio
async def test_duplicate_opened_does_not_mount_second_widget() -> None:
    app = _HostApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)  # type: ignore[arg-type]
        card_id = f"crd_{uuid4()}"
        opened_ev = _mk_opened(card_id)
        await adapter.handle(opened_ev)
        await adapter.handle(opened_ev)
        await pilot.pause()

        transcript = app.query_one("#transcript", VerticalScroll)
        widgets = list(transcript.query(CardWidget))
        # Idempotence preserved from spec-006 — only one widget.
        assert len(widgets) == 1
        assert len(adapter._card_widgets) == 1


@pytest.mark.asyncio
async def test_content_events_dispatch_to_widget_apply() -> None:
    """`card.appended` + `card.set_progress` propagate to the widget.

    Verifies the Protocol seam: adapter handler code (unchanged
    from spec-006) writes through `apply_append` on the widget;
    `set_progress` reaches the widget's `apply_event`.
    """
    app = _HostApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)  # type: ignore[arg-type]
        card_id = f"crd_{uuid4()}"
        await adapter.handle(_mk_opened(card_id, kind="markdown", title="live"))
        await pilot.pause()
        widget = adapter._card_widgets[CardId(card_id)]
        assert isinstance(widget, CardWidget)

        # `card.appended` — Protocol seam writes via `apply_append`.
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_APPENDED,
                {"card_id": card_id, "chunk": "chunk-a"},
            )
        )
        await pilot.pause()
        assert widget.content == "chunk-a"
        assert widget.state == "running"

        # `card.set_progress` on a `markdown` widget is a no-op on
        # the body renderer (progress kind only). Verify the call
        # doesn't crash the widget — it flows through `apply_event`.
        # For real coverage, open a `progress`-kind card too.
        progress_id = f"crd_{uuid4()}"
        await adapter.handle(
            _mk_opened(
                progress_id,
                kind="progress",
                title="p",
            )
        )
        await pilot.pause()
        await adapter.handle(
            _mk_ui_event(
                WIRE_CARD_SET_PROGRESS,
                {
                    "card_id": progress_id,
                    "current": 3,
                    "total": 10,
                    "message": "step 3/10",
                },
            )
        )
        await pilot.pause()
        progress_widget = adapter._card_widgets[CardId(progress_id)]
        assert isinstance(progress_widget, CardWidget)
        # The progress renderer bar reflects the update.
        from textual.widgets import ProgressBar

        bars = list(progress_widget.query(ProgressBar))
        assert len(bars) == 1
        assert bars[0].progress == 3.0
        assert bars[0].total == 10.0
