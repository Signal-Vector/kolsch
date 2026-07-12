"""Pilot tests for `UserChatbox`.

Modularity rule (spec Design Principles #2): the widget is tested
in isolation via `mock_app_factory` from the package conftest —
no other widget, no theme.tcss, no app-level wiring is required
for the assertions below to hold.

What we assert:

1. Constructor accepts ``(text: str, timestamp: datetime)`` and
   the widget mounts cleanly inside a minimal Textual app.
2. The border title is exactly ``"You · HH:MM:SS"`` formatted
   from the supplied timestamp.
3. Body text is rendered **literally** — input containing
   backticks (which Markdown would parse as code fences) is
   preserved verbatim.
4. The `DEFAULT_CSS` on the class references ``$user-border``
   with a rounded border. That variable is theme-provided; the
   widget must not hardcode a colour.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cli.chat.widgets.user_chatbox import UserChatbox


@pytest.mark.asyncio
async def test_user_chatbox_mounts_with_border_title(mock_app_factory) -> None:
    ts = datetime(2026, 7, 4, 14, 30, 45, tzinfo=UTC)
    widget = UserChatbox("hello world", ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        # The widget composed the title from the timestamp verbatim.
        assert widget.border_title == "You · 14:30:45"


@pytest.mark.asyncio
async def test_user_chatbox_renders_literal_text_with_backticks(
    mock_app_factory,
) -> None:
    """Backticks (markdown code) and brackets (Rich markup) must
    survive as literal characters — the user chatbox is NOT a
    Markdown widget."""

    ts = datetime(2026, 7, 4, 9, 0, 0, tzinfo=UTC)
    raw = "check `foo.py` and [bold]not bold[/bold]"
    widget = UserChatbox(raw, ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Render the widget and capture the visible text.
        rendered = widget.render()
        # `Static.render()` returns a Rich renderable. Convert to a
        # plain string via the console's segments so we can grep the
        # actual glyphs the terminal would see.
        console = widget.app.console
        with console.capture() as cap:
            console.print(rendered, end="")
        text = cap.get()

        # Every literal character survived — no markup was consumed
        # and no fenced code was rendered.
        assert "`foo.py`" in text
        assert "[bold]not bold[/bold]" in text


@pytest.mark.asyncio
async def test_user_chatbox_default_css_uses_user_border_variable(
    mock_app_factory,
) -> None:
    """The widget's DEFAULT_CSS must reference the ``$user-border``
    theme variable (not a hardcoded hex) and declare a rounded
    border. This is the modularity contract: swap the theme, the
    border colour changes without touching the widget."""

    ts = datetime(2026, 7, 4, 0, 0, 0, tzinfo=UTC)
    widget = UserChatbox("x", ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        css = UserChatbox.DEFAULT_CSS
        assert "$user-border" in css
        assert "border: round" in css


@pytest.mark.asyncio
async def test_user_chatbox_title_pads_single_digit_time(
    mock_app_factory,
) -> None:
    """Border title uses HH:MM:SS with zero-padding — never
    ``9:5:1``."""

    ts = datetime(2026, 1, 1, 9, 5, 1, tzinfo=UTC)
    widget = UserChatbox("morning", ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget.border_title == "You · 09:05:01"
