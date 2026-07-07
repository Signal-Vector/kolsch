"""Pilot tests for the ``InfoBar`` widget.

The InfoBar is the persistent bottom status strip that shows
``agent · model [· session] · branch`` on the left and the cwd
basename on the right. Per the spec (``process/spec-textual-chat-ui.md``
→ Technical Context → Status bar → InfoBar) it collapses segments in
priority order under narrow terminals — cwd drops first, then branch,
then session; the ``.identity`` (agent + model) slot never drops.

We drive the widget through ``App.run_test()`` at four terminal widths
(100, 80, 60, 40) using the ``fake_app_factory`` fixture from
``tests/cli/chat/conftest.py``. The factory does NOT load
``theme.tcss``; a widget that only works with the theme loaded is a
widget with hidden coupling.
"""

from __future__ import annotations

import pytest
from textual.widgets import Static

from cli.chat.widgets.info_bar import InfoBar


# ── Basic mount / composition ────────────────────────────────────────


@pytest.mark.asyncio
async def test_info_bar_mounts_with_default_reactives(fake_app_factory) -> None:
    """Widget mounts cleanly with all reactives at their empty defaults."""
    bar = InfoBar()
    app = fake_app_factory(bar)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        # Four segments compose regardless of content.
        assert len(bar.query(Static)) == 4
        # Identity slot is empty (agent + model both empty).
        assert str(bar.query_one("#info-identity", Static).render()) == ""
        assert str(bar.query_one("#info-session", Static).render()) == ""
        assert str(bar.query_one("#info-branch", Static).render()) == ""
        assert str(bar.query_one("#info-cwd", Static).render()) == ""


@pytest.mark.asyncio
async def test_info_bar_is_docked_bottom(fake_app_factory) -> None:
    """DEFAULT_CSS docks the InfoBar to the bottom of its parent."""
    bar = InfoBar()
    app = fake_app_factory(bar)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        # y=23 is the bottom row in a 24-row viewport.
        assert bar.region.y == 23
        assert bar.region.height == 1
        assert bar.region.width == 100


# ── Reactive updates ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_info_bar_reactives_populate_segments(fake_app_factory) -> None:
    """Setting the reactives updates the corresponding Static text."""
    bar = InfoBar()
    app = fake_app_factory(bar)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        bar.agent = "butler"
        bar.model = "sonnet-4"
        bar.session = "sess-42"
        bar.branch = "main"
        bar.cwd = "kolsch"
        await pilot.pause()

        assert str(bar.query_one("#info-identity", Static).render()) == "butler · sonnet-4"
        assert str(bar.query_one("#info-session", Static).render()) == "sess-42"
        assert str(bar.query_one("#info-branch", Static).render()) == "main"
        assert str(bar.query_one("#info-cwd", Static).render()) == "kolsch"


@pytest.mark.asyncio
async def test_info_bar_identity_handles_missing_pieces(fake_app_factory) -> None:
    """Identity slot drops empty pieces so we never render a bare ' · '."""
    bar = InfoBar()
    app = fake_app_factory(bar)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        bar.agent = "butler"
        # model left empty
        await pilot.pause()
        assert str(bar.query_one("#info-identity", Static).render()) == "butler"

        bar.agent = ""
        bar.model = "sonnet-4"
        await pilot.pause()
        assert str(bar.query_one("#info-identity", Static).render()) == "sonnet-4"


@pytest.mark.asyncio
async def test_info_bar_empty_reactives_render_empty(fake_app_factory) -> None:
    """An unset reactive keeps its segment empty (no placeholder text)."""
    bar = InfoBar()
    app = fake_app_factory(bar)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        # Populate only identity.
        bar.agent = "butler"
        bar.model = "sonnet-4"
        await pilot.pause()

        assert str(bar.query_one("#info-identity", Static).render()) == "butler · sonnet-4"
        assert str(bar.query_one("#info-session", Static).render()) == ""
        assert str(bar.query_one("#info-branch", Static).render()) == ""
        assert str(bar.query_one("#info-cwd", Static).render()) == ""


# ── Width-based collapse (framework-native `display: none` toggle) ───


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "width,expected_visible",
    [
        # width, set-of-segment-ids-that-must-be-displayed
        (100, {"info-identity", "info-session", "info-branch", "info-cwd"}),
        (80, {"info-identity", "info-session", "info-branch", "info-cwd"}),
        (60, {"info-identity", "info-session", "info-branch"}),
        (40, {"info-identity", "info-session"}),
    ],
)
async def test_info_bar_segments_collapse_by_width(
    fake_app_factory, width: int, expected_visible: set[str]
) -> None:
    """At narrower widths the droppable segments hide in priority order.

    Priority (drop last first): identity > session > branch > cwd. The
    ``.identity`` segment never drops — it carries the agent+model
    invariant.
    """
    bar = InfoBar()
    app = fake_app_factory(bar)
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause()
        bar.agent = "butler"
        bar.model = "sonnet-4"
        bar.session = "sess-42"
        bar.branch = "main"
        bar.cwd = "kolsch"
        await pilot.pause()

        seen_visible = {
            st.id
            for st in bar.query(Static)
            if st.display and st.id is not None
        }
        assert seen_visible == expected_visible


@pytest.mark.asyncio
async def test_info_bar_identity_always_present(fake_app_factory) -> None:
    """Even at the narrowest tested width, identity keeps displaying."""
    bar = InfoBar()
    app = fake_app_factory(bar)
    async with app.run_test(size=(40, 24)) as pilot:
        await pilot.pause()
        bar.agent = "butler"
        bar.model = "sonnet-4"
        await pilot.pause()
        assert bar.query_one("#info-identity", Static).display is True


# ── CSS smoke: DEFAULT_CSS is a class attribute (per spec rule) ──────


def test_info_bar_declares_default_css() -> None:
    """Widget must own its layout via a ``DEFAULT_CSS`` class attribute.

    The spec explicitly forbids widget-specific rules in theme.tcss;
    every widget stands on its own DEFAULT_CSS.
    """
    assert isinstance(InfoBar.DEFAULT_CSS, str)
    assert "dock: bottom" in InfoBar.DEFAULT_CSS
    assert "height: 1" in InfoBar.DEFAULT_CSS
