"""spec-007 Test 1 — `CardWidget` mount + state transitions.

Six Pilot passes exercising the state machine:

1. Mount a `CardWidget(card_id, kind="markdown", title="t")` in a
   Pilot session. Assert `.state == "initializing"`, no CSS state
   class, header glyph `⠋`.
2. Dispatch `card.appended("hi")` via `apply_event`. Assert state
   flips to `"running"`, header glyph stays kind-appropriate,
   body contains "hi".
3. Dispatch `card.closed(final_status="success")`. Assert state →
   `"success"`; glyph `✓`; auto-collapses within 1.2s.
4. Same but `final_status="error"` + `error_message="oops"`.
   Assert `.-error` class present; footer shows `✗ error: oops`;
   stays expanded.
5. Same but `final_status="cancelled"`. Assert `.-cancelled`
   class, glyph `─`.
6. `card.status="paused"` → `.-paused` class, glyph `⚠`.
"""

from __future__ import annotations

from typing import Any

import pytest

from cli.chat.widgets.card_widget import CardFooter, CardHeader, CardWidget


class _FakeEvent:
    """Duck-typed event data for `CardWidget.apply_event`."""

    __slots__ = ("kind", "payload", "event_id")

    def __init__(self, kind: str, payload: dict[str, Any], event_id: str = "ev-1") -> None:
        self.kind = kind
        self.payload = payload
        self.event_id = event_id


def _header_text(widget: CardWidget) -> str:
    """Extract the rendered header text (glyph + title)."""
    header = widget.query_one(CardHeader)
    return str(header.render())


def _footer_text(widget: CardWidget) -> str:
    footer = widget.query_one(CardFooter)
    return str(footer.render())


@pytest.mark.asyncio
async def test_mount_initializing_state(fake_app_factory) -> None:
    widget = CardWidget(card_id="crd_test1", kind="markdown", title="t")
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget.state == "initializing"
        # No CSS state class (default state).
        for cls in ("-queued", "-paused", "-cancelling", "-error", "-cancelled"):
            assert not widget.has_class(cls)
        # Initial glyph for a markdown card = ⠋ (running spinner).
        assert "⠋" in _header_text(widget)
        # Title threads through the header.
        assert "t" in _header_text(widget)


@pytest.mark.asyncio
async def test_appended_transitions_to_running(fake_app_factory) -> None:
    widget = CardWidget(card_id="crd_test2", kind="markdown", title="hello")
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        widget.apply_event(_FakeEvent(
            "kolsch.card.appended",
            {"chunk": "hi"},
            event_id="ev-append-1",
        ))
        await pilot.pause()
        assert widget.state == "running"
        # `.content` tracks the applied append.
        assert widget.content == "hi"
        # Header glyph stays at the running kind-default (⠋ for markdown).
        assert "⠋" in _header_text(widget)


@pytest.mark.asyncio
async def test_closed_success_auto_collapses(fake_app_factory) -> None:
    widget = CardWidget(card_id="crd_test3", kind="markdown", title="done")
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget.collapsed is False
        widget.apply_event(_FakeEvent(
            "kolsch.card.closed",
            {"final_status": "success", "error_message": None},
            event_id="ev-close-1",
        ))
        await pilot.pause()
        assert widget.state == "success"
        assert "✓" in _header_text(widget)
        # Auto-collapse after 1s — wait 1.2s.
        await pilot.pause(1.2)
        assert widget.collapsed is True


@pytest.mark.asyncio
async def test_closed_error_stays_expanded_with_footer(fake_app_factory) -> None:
    widget = CardWidget(card_id="crd_test4", kind="markdown", title="fail")
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        widget.apply_event(_FakeEvent(
            "kolsch.card.closed",
            {"final_status": "error", "error_message": "oops"},
            event_id="ev-close-err",
        ))
        await pilot.pause()
        assert widget.state == "error"
        assert widget.has_class("-error")
        assert "✗ error: oops" in _footer_text(widget)
        # Error stays expanded — no auto-collapse.
        await pilot.pause(1.2)
        assert widget.collapsed is False


@pytest.mark.asyncio
async def test_closed_cancelled_applies_class_and_glyph(fake_app_factory) -> None:
    widget = CardWidget(card_id="crd_test5", kind="markdown", title="c")
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        widget.apply_event(_FakeEvent(
            "kolsch.card.closed",
            {"final_status": "cancelled", "error_message": None},
            event_id="ev-close-cancel",
        ))
        await pilot.pause()
        assert widget.state == "cancelled"
        assert widget.has_class("-cancelled")
        assert "─" in _header_text(widget)


@pytest.mark.asyncio
async def test_status_paused_applies_class_and_glyph(fake_app_factory) -> None:
    widget = CardWidget(card_id="crd_test6", kind="markdown", title="p")
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        widget.apply_event(_FakeEvent(
            "kolsch.card.status",
            {"status": "paused"},
            event_id="ev-status-p",
        ))
        await pilot.pause()
        assert widget.state == "paused"
        assert widget.has_class("-paused")
        assert "⚠" in _header_text(widget)
