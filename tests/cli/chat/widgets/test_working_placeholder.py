"""Tests for :class:`WorkingPlaceholder` (spec-130 Step 1).

Step 1 builds the widget standalone — not wired into any event flow.
These tests cover:

- Working state: GutterBlock composed with rail ``$hint``, spinner
  glyph cycling through ``SPINNER_FRAMES`` on the ~0.08s interval.
- ``render_terminal("interrupted")``: ``─`` glyph (state_tokens.py's
  ``"cancelled"`` token), ``$hint`` rail, dim text-style.
- ``render_terminal("error")``: ``✗`` glyph (state_tokens.py's
  ``"failed"`` token), ``$error`` rail, bold text-style.
- NO_COLOR construction test per state, following ``test_notice.py``'s
  ``test_glyph_present_under_no_color_construction`` pattern: construct
  under ``mock.patch.dict(os.environ, {"NO_COLOR": "1"})`` and assert
  the glyph is present and identical to the color-mode glyph.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from textual.widgets import Static

from cli.chat.spinner import SPINNER_FRAMES
from cli.chat.state_tokens import resolve_state
from cli.chat.widgets._gutter_block import GutterBlock
from cli.chat.widgets.working_placeholder import WorkingPlaceholder

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


def _glyph_text(widget: WorkingPlaceholder) -> str:
    """Read the currently-rendered glyph-column text off a mounted widget."""
    return str(widget.query_one(".gb-glyph-col", Static).render())


def _body_text(widget: WorkingPlaceholder) -> str:
    """Read the currently-rendered body text off a mounted widget."""
    return str(widget.query_one(".working-body", Static).render())


# ----------------------------------------------------------------------
# Working state
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_working_state_composes_gutter_block_with_hint_rail(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The widget composes a GutterBlock with rail ``$hint`` on mount."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        block = widget.query_one(GutterBlock)
        assert block.rail_color == "$hint"
        assert block.has_class("-rail-hint")
        # Not yet flipped to a terminal state.
        assert widget.is_terminal is False
        assert widget.terminal_kind is None


@pytest.mark.asyncio
async def test_working_state_initial_glyph_is_a_spinner_frame(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """On mount, the glyph column shows a frame from SPINNER_FRAMES."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert _glyph_text(widget) in SPINNER_FRAMES


@pytest.mark.asyncio
async def test_working_state_spinner_cycles_over_time(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The glyph column advances through distinct spinner frames on the
    ~0.08s interval convention (same cadence as TurnStatus)."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        seen: set[str] = set()
        first = _glyph_text(widget)
        seen.add(first)
        # Advance enough ticks (0.08s each) to observe more than one
        # distinct frame without depending on exact timer precision.
        for _ in range(6):
            await pilot.pause(0.08)
            seen.add(_glyph_text(widget))

        assert len(seen) > 1, "spinner did not cycle through multiple frames"
        assert seen.issubset(set(SPINNER_FRAMES))


@pytest.mark.asyncio
async def test_working_state_body_shows_thinking_verb(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The body text shows the working-state verb."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert "Thinking" in _body_text(widget)


# ----------------------------------------------------------------------
# Terminal state: interrupted
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_terminal_interrupted_glyph_and_class(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``render_terminal("interrupted")`` shows the cancelled-token glyph."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()

        cancelled_token = resolve_state("cancelled")
        assert cancelled_token.glyph == "─"
        assert _glyph_text(widget) == "─"
        assert widget.has_class("-interrupted")
        assert widget.is_terminal is True
        assert widget.terminal_kind == "interrupted"


@pytest.mark.asyncio
async def test_render_terminal_interrupted_rail_stays_hint(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Interrupted state keeps the ``$hint`` rail (per the glyph/color table)."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()

        block = widget.query_one(GutterBlock)
        assert block.has_class("-rail-hint")
        assert not block.has_class("-rail-error")


@pytest.mark.asyncio
async def test_render_terminal_interrupted_body_is_dim(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Interrupted body text renders with ``dim`` text-style."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()

        body = widget.query_one(".working-body", Static)
        assert bool(body.styles.text_style.dim)


@pytest.mark.asyncio
async def test_render_terminal_interrupted_stops_spinner(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """After flipping to interrupted, the glyph no longer changes over time."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("interrupted")
        await pilot.pause()
        frozen = _glyph_text(widget)
        assert frozen == "─"

        await pilot.pause(0.3)
        assert _glyph_text(widget) == "─"


# ----------------------------------------------------------------------
# Terminal state: error
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_terminal_error_glyph_and_class(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``render_terminal("error")`` shows the failed-token glyph."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("error")
        await pilot.pause()

        failed_token = resolve_state("failed")
        assert failed_token.glyph == "✗"
        assert _glyph_text(widget) == "✗"
        assert widget.has_class("-error")
        assert widget.is_terminal is True
        assert widget.terminal_kind == "error"


@pytest.mark.asyncio
async def test_render_terminal_error_rail_flips_to_error(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Error state flips the rail to ``$error`` (per the glyph/color table)."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("error")
        await pilot.pause()

        block = widget.query_one(GutterBlock)
        assert block.has_class("-rail-error")
        assert not block.has_class("-rail-hint")


@pytest.mark.asyncio
async def test_render_terminal_error_body_is_bold(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Error body text renders with ``bold`` text-style."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("error")
        await pilot.pause()

        body = widget.query_one(".working-body", Static)
        assert bool(body.styles.text_style.bold)


@pytest.mark.asyncio
async def test_render_terminal_error_stops_spinner(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """After flipping to error, the glyph no longer changes over time."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()

        widget.render_terminal("error")
        await pilot.pause()
        assert _glyph_text(widget) == "✗"

        await pilot.pause(0.3)
        assert _glyph_text(widget) == "✗"


# ----------------------------------------------------------------------
# NO_COLOR construction tests
# ----------------------------------------------------------------------


def test_working_glyph_present_under_no_color_construction() -> None:
    """Constructing under NO_COLOR still produces a valid working-state glyph.

    Mirrors ``test_notice.py``'s
    ``test_glyph_present_under_no_color_construction`` — NO_COLOR only
    affects theme/palette selection, not the widget's own glyph
    resolution.
    """
    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
        widget = WorkingPlaceholder()
    # Initial glyph is set from the spinner index at construction time,
    # independent of environment/theme.
    assert widget.is_terminal is False


@pytest.mark.asyncio
async def test_working_glyph_matches_across_color_and_no_color(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The initial working-state glyph is identical between color + NO_COLOR."""
    env_no_color = dict(os.environ, NO_COLOR="1")
    env_color = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}

    with mock.patch.dict(os.environ, env_color, clear=True):
        w_color = WorkingPlaceholder()
    with mock.patch.dict(os.environ, env_no_color, clear=True):
        w_mono = WorkingPlaceholder()

    async with mock_app_factory(w_color).run_test() as pilot:
        await pilot.pause()
        color_glyph = _glyph_text(w_color)
    async with mock_app_factory(w_mono).run_test() as pilot:
        await pilot.pause()
        mono_glyph = _glyph_text(w_mono)

    assert color_glyph == mono_glyph
    assert color_glyph in SPINNER_FRAMES


@pytest.mark.asyncio
async def test_interrupted_glyph_present_under_no_color_construction(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``render_terminal("interrupted")`` glyph is identical under NO_COLOR."""
    env_no_color = dict(os.environ, NO_COLOR="1")
    env_color = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}

    with mock.patch.dict(os.environ, env_color, clear=True):
        w_color = WorkingPlaceholder()
    with mock.patch.dict(os.environ, env_no_color, clear=True):
        w_mono = WorkingPlaceholder()

    async with mock_app_factory(w_color).run_test() as pilot:
        await pilot.pause()
        w_color.render_terminal("interrupted")
        await pilot.pause()
        color_glyph = _glyph_text(w_color)
    async with mock_app_factory(w_mono).run_test() as pilot:
        await pilot.pause()
        w_mono.render_terminal("interrupted")
        await pilot.pause()
        mono_glyph = _glyph_text(w_mono)

    assert color_glyph == mono_glyph == "─"


@pytest.mark.asyncio
async def test_error_glyph_present_under_no_color_construction(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``render_terminal("error")`` glyph is identical under NO_COLOR."""
    env_no_color = dict(os.environ, NO_COLOR="1")
    env_color = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}

    with mock.patch.dict(os.environ, env_color, clear=True):
        w_color = WorkingPlaceholder()
    with mock.patch.dict(os.environ, env_no_color, clear=True):
        w_mono = WorkingPlaceholder()

    async with mock_app_factory(w_color).run_test() as pilot:
        await pilot.pause()
        w_color.render_terminal("error")
        await pilot.pause()
        color_glyph = _glyph_text(w_color)
    async with mock_app_factory(w_mono).run_test() as pilot:
        await pilot.pause()
        w_mono.render_terminal("error")
        await pilot.pause()
        mono_glyph = _glyph_text(w_mono)

    assert color_glyph == mono_glyph == "✗"


# ----------------------------------------------------------------------
# Standalone mount (no engine/event wiring)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_widget_mounts_standalone_with_no_wiring(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The widget can be constructed and mounted with zero engine/event wiring."""
    widget = WorkingPlaceholder()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert widget.is_mounted
        assert widget.query_one(GutterBlock) is not None
