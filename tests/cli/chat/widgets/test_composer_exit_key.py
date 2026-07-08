"""bug-023 — `Composer._on_key` handling of Ctrl+D.

Widget-level Pilot tests for the three-way branch introduced by
bug-023:

- **Empty composer**: ``ctrl+d`` posts ``Composer.QuitRequested``
  and stops the event (TextArea's own ``delete_right`` binding
  MUST NOT run because there's no text to delete anyway, and the
  app-level ``ctrl+d`` binding would otherwise be shadowed by the
  focused widget's binding chain).
- **Whitespace-only composer**: same as empty. ``"   "`` /
  ``"\n\n"`` are user-invisible content; a user hitting ``ctrl+d``
  in that state expects to quit, not to delete an invisible space.
- **Non-empty composer**: ``ctrl+d`` falls through to
  ``TextArea._on_key`` → binding chain → ``delete_right``. Users
  editing text expect Ctrl+D to work as forward-delete (matches
  every other TextArea in the ecosystem).

The host ``App`` yields the composer via a per-test module-level
subclass rather than the dynamic ``class _Host(type(factory(...)))``
pattern from existing widget tests — the static base class keeps
mypy strict clean without the ``[misc]`` dynamic-base-class
warning while still matching the widget-in-isolation contract.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from cli.chat.widgets.composer import Composer


class _HostApp(App[None]):
    """Minimal host `App` that yields ONE composer and captures quits.

    Rewired per test via ``_composer`` on the instance so each test
    sees an isolated widget; ``captured`` collects
    ``Composer.QuitRequested`` messages so the tests assert on the
    handler's payload directly rather than relying on the app to
    quit (the composer's contract is "post the message" — whether
    the app quits is the ``ChatApp``-only concern covered by
    ``tests/cli/chat/test_composer_quit_message.py``).
    """

    def __init__(self, composer: Composer) -> None:
        super().__init__()
        self._composer = composer
        self.captured: list[Composer.QuitRequested] = []

    def compose(self) -> ComposeResult:
        yield self._composer

    def on_composer_quit_requested(
        self, event: Composer.QuitRequested,
    ) -> None:
        self.captured.append(event)


@pytest.mark.asyncio
async def test_ctrl_d_on_empty_posts_quit_requested() -> None:
    """Ctrl+D on an empty composer posts a `QuitRequested` message."""
    composer = Composer()
    app = _HostApp(composer)
    async with app.run_test() as pilot:
        composer.focus()
        await pilot.pause()
        assert composer.text == ""
        await pilot.press("ctrl+d")
        await pilot.pause()

    assert len(app.captured) == 1
    # Composer text is unchanged — TextArea's delete_right did NOT run.
    assert composer.text == ""


@pytest.mark.asyncio
async def test_ctrl_d_on_whitespace_only_posts_quit_requested() -> None:
    """Whitespace-only text is treated the same as empty for Ctrl+D."""
    composer = Composer()
    app = _HostApp(composer)
    async with app.run_test() as pilot:
        composer.focus()
        composer.text = "   \n\t "
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()

    assert len(app.captured) == 1
    # Whitespace preserved — TextArea's delete_right did NOT run.
    assert composer.text == "   \n\t "


@pytest.mark.asyncio
async def test_ctrl_d_on_non_empty_deletes_right() -> None:
    """Ctrl+D on a non-empty composer falls through to `delete_right`.

    Type ``"hello"``, move the cursor to column 0, press
    ``ctrl+d``. TextArea's binding fires and the character to the
    right of the cursor (``h``) is deleted — text becomes ``"ello"``.
    No ``QuitRequested`` is posted.
    """
    composer = Composer()
    app = _HostApp(composer)
    async with app.run_test() as pilot:
        composer.focus()
        composer.text = "hello"
        await pilot.pause()
        # Move cursor to the start of the document (row 0, col 0).
        composer.move_cursor((0, 0))
        await pilot.pause()
        await pilot.press("ctrl+d")
        await pilot.pause()

    assert app.captured == []
    assert composer.text == "ello"
