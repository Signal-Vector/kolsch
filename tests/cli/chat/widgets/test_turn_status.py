"""Pilot tests for the ``TurnStatus`` transient status widget.

See ``process/spec-textual-chat-ui.md`` → Technical Context →
``TurnStatus`` for the reactive contract this test verifies:

- Setting ``verb`` on a mounted widget adds the ``.-active`` class
  (which the DEFAULT_CSS uses to flip ``display: none`` → ``block``).
- The ``#elapsed`` segment stays hidden until 2s have elapsed; after
  ``pilot.pause(2.5)`` the ``.-show-elapsed`` class is present and the
  segment is visible.
- Setting ``awaiting_approval = True`` toggles ``.-awaiting-approval``
  which reveals the docked-right ``#warning`` slot.
- Clearing ``verb`` deactivates the widget and stops the timers so idle
  sessions don't tick a clock forever.

Tests use the ``fake_app_factory`` fixture from ``tests/cli/chat/conftest.py``
so each widget is mounted in a bare host app — the widget must render
correctly from its own DEFAULT_CSS with no external theme loaded.
"""

from __future__ import annotations

import pytest
from textual.widgets import Static

from cli.chat.widgets.turn_status import (
    _SPINNER_FRAMES,
    TurnStatus,
    _format_elapsed,
)


def test_format_elapsed_under_a_minute() -> None:
    """Sub-minute values render as ``(NNs)``."""
    assert _format_elapsed(0) == "(0s)"
    assert _format_elapsed(7) == "(7s)"
    assert _format_elapsed(59) == "(59s)"


def test_format_elapsed_over_a_minute() -> None:
    """Multi-minute values render as ``(Mm SSs)`` with zero-padded seconds."""
    assert _format_elapsed(60) == "(1m 00s)"
    assert _format_elapsed(75) == "(1m 15s)"
    assert _format_elapsed(3 * 60 + 4) == "(3m 04s)"


def test_spinner_frame_set_is_the_documented_braille_cycle() -> None:
    """Spec locks the 10-frame Braille cycle; guard against typo drift."""
    assert _SPINNER_FRAMES == "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    assert len(_SPINNER_FRAMES) == 10


@pytest.mark.asyncio
async def test_setting_verb_activates_widget(fake_app_factory) -> None:
    """A non-empty ``verb`` adds ``.-active`` and reveals the row."""
    widget = TurnStatus()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Idle: no verb, no active class, display collapsed via CSS.
        assert not widget.has_class("-active")

        widget.verb = "Thinking…"
        await pilot.pause()

        assert widget.has_class("-active")
        rendered = str(widget.query_one("#spinner-verb", Static).render())
        # Frame + verb; frame is one of the documented Braille chars.
        assert "Thinking…" in rendered
        assert any(frame in rendered for frame in _SPINNER_FRAMES)


@pytest.mark.asyncio
async def test_target_reactive_updates_target_static(fake_app_factory) -> None:
    """Setting ``target`` writes into ``#target`` (rendered dim italic via CSS)."""
    widget = TurnStatus()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.verb = "Calling read_file"
        widget.target = "path=/tmp/example.txt"
        await pilot.pause()

        rendered = str(widget.query_one("#target", Static).render())
        assert rendered == "path=/tmp/example.txt"


@pytest.mark.asyncio
async def test_elapsed_hidden_before_two_seconds(fake_app_factory) -> None:
    """The 2-second gate keeps ``#elapsed`` invisible for the first tick."""
    widget = TurnStatus()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.verb = "Thinking…"
        # Short pause: gate has not fired yet.
        await pilot.pause(0.3)

        assert not widget.has_class("-show-elapsed")
        assert widget.show_elapsed is False


@pytest.mark.asyncio
async def test_elapsed_revealed_after_two_seconds(fake_app_factory) -> None:
    """After ``pilot.pause(2.5)`` the ``.-show-elapsed`` class flips on.

    This is the spec's canonical test: ``set verb → pilot.pause(2.5) →
    #elapsed visible``.
    """
    widget = TurnStatus()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.verb = "Thinking…"
        # Give the 2s gate timer a chance to fire; the 1Hz tick will
        # also have run at least once so elapsed_seconds >= 1.
        await pilot.pause(2.5)

        assert widget.show_elapsed is True
        assert widget.has_class("-show-elapsed")
        assert widget.elapsed_seconds >= 1
        rendered = str(widget.query_one("#elapsed", Static).render())
        assert rendered.startswith("(") and rendered.endswith("s)")


@pytest.mark.asyncio
async def test_awaiting_approval_reveals_warning_slot(fake_app_factory) -> None:
    """Flipping ``awaiting_approval`` toggles the ``.-awaiting-approval`` class."""
    widget = TurnStatus()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.verb = "Calling shell"
        widget.awaiting_approval = True
        await pilot.pause()

        assert widget.has_class("-awaiting-approval")
        # The warning static remains in the DOM — the CSS class flips
        # it from ``display: none`` to ``display: block``.
        warning = widget.query_one("#warning", Static)
        assert warning is not None

        widget.awaiting_approval = False
        await pilot.pause()
        assert not widget.has_class("-awaiting-approval")


@pytest.mark.asyncio
async def test_clearing_verb_deactivates_widget(fake_app_factory) -> None:
    """Emptying ``verb`` removes ``.-active`` and resets elapsed state."""
    widget = TurnStatus()
    app = fake_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.verb = "Thinking…"
        await pilot.pause(2.5)
        assert widget.has_class("-active")

        widget.verb = ""
        await pilot.pause()

        assert not widget.has_class("-active")
        assert not widget.has_class("-show-elapsed")
        assert widget.elapsed_seconds == 0
        assert widget.show_elapsed is False
