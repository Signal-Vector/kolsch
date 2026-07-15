"""Pilot tests for ``SlashPopup``.

The popup reads its command catalog from
``cli._slash.COMMAND_REGISTRY``; the tests treat that registry as
the source of truth (i.e. they don't hard-code the list of
commands — a new registered command would appear in the popup
automatically).

What we assert:

1. Empty ``prefix`` → popup hidden (no ``-visible`` class).
2. Prefix not starting with ``/`` → popup hidden.
3. Prefix ``/`` → popup visible with every registered command.
4. Prefix ``/h`` → popup visible with the ``/help`` row only.
5. Prefix ``/xyz`` (no match) → popup visible with a "no match"
   row.
6. Aliases (e.g. ``/quit``) surface as their own row when the
   prefix matches them.
"""

from __future__ import annotations

import pytest
from textual.widgets import Static

from cli._slash import COMMAND_REGISTRY
from cli.chat.widgets.slash_popup import SlashPopup


def _visible_rows(widget: SlashPopup) -> list[str]:
    """Return the rendered text of every mounted row Static.

    Called after `pilot.pause()` so watch_prefix has time to remount.
    """
    return [str(s.render()) for s in widget.query(Static)]


@pytest.mark.asyncio
async def test_empty_prefix_hides_popup(mock_app_factory) -> None:
    popup = SlashPopup()
    app = mock_app_factory(popup)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not popup.has_class("-visible")
        assert _visible_rows(popup) == []


@pytest.mark.asyncio
async def test_non_slash_prefix_hides_popup(mock_app_factory) -> None:
    popup = SlashPopup()
    app = mock_app_factory(popup)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup.prefix = "hello"
        await pilot.pause()
        assert not popup.has_class("-visible")


@pytest.mark.asyncio
async def test_slash_prefix_shows_every_command(mock_app_factory) -> None:
    popup = SlashPopup()
    app = mock_app_factory(popup)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup.prefix = "/"
        await pilot.pause()
        assert popup.has_class("-visible")
        rows = _visible_rows(popup)
        # One row per canonical registry entry (aliases append
        # their own rows too when they start with `/`).
        for cmd in COMMAND_REGISTRY.values():
            assert any(cmd.name in row for row in rows), f"missing row for {cmd.name}"


@pytest.mark.asyncio
async def test_prefix_filters_to_matching_command(mock_app_factory) -> None:
    popup = SlashPopup()
    app = mock_app_factory(popup)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup.prefix = "/hel"
        await pilot.pause()
        rows = _visible_rows(popup)
        # Exactly one command name row + one usage footer row.
        assert any("/help" in r for r in rows)
        # And nothing else that starts with `/hel` in the registry.
        other_names = [c.name for c in COMMAND_REGISTRY.values() if c.name != "/help"]
        for name in other_names:
            assert not any(r.startswith(f"{name:<12}") for r in rows), (
                f"{name} leaked into /hel matches"
            )


@pytest.mark.asyncio
async def test_no_match_shows_empty_row(mock_app_factory) -> None:
    popup = SlashPopup()
    app = mock_app_factory(popup)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup.prefix = "/xyzzy"
        await pilot.pause()
        assert popup.has_class("-visible")
        rows = _visible_rows(popup)
        assert len(rows) == 1
        assert "no slash command matches" in rows[0]


@pytest.mark.asyncio
async def test_alias_shows_own_row(mock_app_factory) -> None:
    popup = SlashPopup()
    app = mock_app_factory(popup)
    async with app.run_test() as pilot:
        await pilot.pause()
        popup.prefix = "/quit"
        await pilot.pause()
        rows = _visible_rows(popup)
        # `/quit` is an alias of `/exit`; it should surface as its
        # own row rather than being hidden.
        assert any("/quit" in r for r in rows)
