"""Pilot tests for `Composer` (the bottom-docked chat input).

Covers the widget's public behavioural contract in isolation:

- Placeholder text matches the spec (default + awaiting-approval swap).
- `max-height: 6` in `DEFAULT_CSS` bounds the visible region even when
  the user types more than six lines of content.
- Pressing Enter posts a `Composer.Submitted` message carrying the
  composer's current text.
- Setting `awaiting_approval=True` disables the widget and swaps the
  placeholder; flipping it back restores the default.

Each test wraps the composer in a minimal host `App` via the
`mock_app_factory` fixture from `tests/cli/chat/conftest.py`, so
nothing here depends on `theme.tcss`, other widgets, or the event
adapter.
"""

from __future__ import annotations

import pytest

from cli.chat.widgets.composer import Composer


@pytest.mark.asyncio
async def test_default_placeholder_matches_spec(mock_app_factory) -> None:
    """Fresh composer shows the spec-mandated placeholder text."""
    composer = Composer()
    app = mock_app_factory(composer)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Placeholder hints at newline keys since Shift+Enter is
        # unreliable across terminals (iTerm2 / Terminal.app often
        # collapse it to plain Enter). Alt+Enter and Ctrl+J are the
        # portable fallbacks the composer also accepts. `/help`
        # is the entry point to slash-command discovery until the
        # follow-up spec wires the completer.
        # bug-038: Ctrl+C is the copy binding again (Textual Screen
        # default at site-packages/textual/screen.py:271). The legacy
        # Ctrl+Shift+C workaround was removed with the app-level
        # Ctrl+C cancel override that had shadowed it.
        assert composer.placeholder == (
            "Message… (Enter=send · Alt+Enter=newline · "
            "Ctrl+C=copy · /help · Ctrl+D or 'exit' to quit)"
        )
        assert composer.disabled is False


@pytest.mark.asyncio
async def test_max_height_enforced_after_typing_eight_lines(
    mock_app_factory,
) -> None:
    """CSS `max-height: 6` bounds the widget region at six rows.

    The document may hold eight lines of content, but the outer widget
    region — the box the user sees — is clamped to 6 rows because
    `DEFAULT_CSS` says so. The widget itself does not clamp the
    document; Textual scrolls it internally.
    """
    composer = Composer()
    app = mock_app_factory(composer)
    async with app.run_test(size=(80, 20)) as pilot:
        composer.focus()
        await pilot.pause()
        # Load eight lines of content directly (avoids terminal
        # shift+enter quirks — this test is about the CSS clamp, not
        # key routing).
        composer.text = "\n".join(f"line {i}" for i in range(8))
        await pilot.pause()
        # Document holds all eight lines…
        assert composer.text.count("\n") == 7
        # …but the visible region on the screen is capped at 6 rows.
        assert composer.region.height <= 6


@pytest.mark.asyncio
async def test_enter_posts_submitted_message_with_content(
    mock_app_factory,
) -> None:
    """Pressing Enter posts `Composer.Submitted` with the current text."""
    captured: list[str] = []

    class _Host(type(mock_app_factory(Composer()))):
        def on_composer_submitted(self, event: Composer.Submitted) -> None:
            captured.append(event.content)

    composer = Composer()
    app = _Host()
    # Swap in our composer so the host yields the instance we assert on.
    app.compose = lambda: iter([composer])  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        composer.focus()
        await pilot.pause()
        composer.text = "hello world"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert captured == ["hello world"]
    # Enter must NOT have inserted a newline into the document.
    assert "\n" not in composer.text


@pytest.mark.asyncio
async def test_awaiting_approval_disables_and_swaps_placeholder(
    mock_app_factory,
) -> None:
    """Toggling `awaiting_approval` disables + swaps; toggling back restores."""
    composer = Composer()
    app = mock_app_factory(composer)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert composer.disabled is False
        assert composer.placeholder == Composer.DEFAULT_PLACEHOLDER

        composer.awaiting_approval = True
        await pilot.pause()
        assert composer.disabled is True
        assert composer.placeholder == Composer.AWAITING_PLACEHOLDER

        composer.awaiting_approval = False
        await pilot.pause()
        assert composer.disabled is False
        assert composer.placeholder == Composer.DEFAULT_PLACEHOLDER


@pytest.mark.asyncio
async def test_enter_while_awaiting_approval_does_not_submit(
    mock_app_factory,
) -> None:
    """Disabled composer swallows key input and does not post `Submitted`.

    While `awaiting_approval` is `True` the composer is disabled;
    Textual routes no key events to a disabled widget, and the
    composer's own guard (see `_on_key`) is belt-and-braces.
    """
    captured: list[str] = []

    class _Host(type(mock_app_factory(Composer()))):
        def on_composer_submitted(self, event: Composer.Submitted) -> None:
            captured.append(event.content)

    composer = Composer()
    app = _Host()
    app.compose = lambda: iter([composer])  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        composer.text = "should not submit"
        composer.awaiting_approval = True
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    assert captured == []
