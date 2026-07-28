# ruff: noqa: RUF001, RUF002, RUF003
"""Tests for :class:`NoticeWidget` (spec-078 rewire + qol-080).

Post spec-078 the widget is a :class:`Widget` container composing a
:class:`GutterBlock` (rail = severity color, glyph = severity
glyph). Pre-migration the glyph was prepended to the body Static's
text (``"ℹ session resumed"``); post-migration the glyph lives in the
GutterBlock's leading-glyph column and the body Static holds just the
message. The redundant-carriers principle (qol-080) is preserved:
rail + glyph column always render regardless of ``NO_COLOR``.

The tests lock in:
- Per-severity glyph mapping (``SEVERITY_GLYPHS``) — unchanged from
  pre-migration.
- Unknown ``kind`` falls back to ``info``.
- The GutterBlock's ``leading_glyph`` is exactly the severity glyph
  and equals across color + NO_COLOR modes.
- ``.text`` returns the raw body message (no glyph prefix).
- ``.kind`` returns the stored severity.
- ``update_text`` replaces the body in place; the rail + glyph column
  are unaffected.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from cli.chat.widgets._gutter_block import GutterBlock
from cli.chat.widgets.notice import SEVERITY_GLYPHS, NoticeWidget

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


def test_severity_glyphs_locked() -> None:
    """The per-severity glyph mapping is spec-locked."""
    assert SEVERITY_GLYPHS == {"info": "ℹ", "warning": "⚠", "error": "✗"}


def test_text_property_returns_raw_body_no_glyph() -> None:
    """Post spec-078 `.text` is the raw message — glyph is on the rail."""
    widget = NoticeWidget(text="Operation completed", kind="info")
    assert widget.text == "Operation completed"


def test_unknown_kind_falls_back_to_info() -> None:
    """Any unknown kind resolves to ``info`` severity."""
    widget = NoticeWidget(text="Something", kind="mystery")
    assert widget.kind == "info"


@pytest.mark.parametrize(
    ("kind", "glyph"),
    [("info", "ℹ"), ("warning", "⚠"), ("error", "✗")],
)
@pytest.mark.asyncio
async def test_gutter_block_carries_severity_glyph(
    kind: str, glyph: str, mock_app_factory: Callable[[Widget], App[None]]
) -> None:
    """The GutterBlock's leading_glyph is exactly the severity glyph."""
    widget = NoticeWidget(text="Operation completed", kind=kind)
    async with mock_app_factory(widget).run_test() as pilot:
        await pilot.pause()
        block = widget.query_one(GutterBlock)
        assert block.leading_glyph == glyph


@pytest.mark.parametrize(
    ("kind", "glyph"),
    [("info", "ℹ"), ("warning", "⚠"), ("error", "✗")],
)
def test_glyph_present_under_no_color_construction(kind: str, glyph: str) -> None:
    """Construction under NO_COLOR still produces the same glyph.

    NO_COLOR only affects theme selection (`ansi_default` for palette
    variables); the widget's severity resolution + glyph column is
    identical regardless.
    """
    with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
        widget = NoticeWidget(text="Operation completed", kind=kind)
    assert widget.kind == kind
    assert SEVERITY_GLYPHS[kind] == glyph


@pytest.mark.parametrize(
    ("kind", "glyph"),
    [("info", "ℹ"), ("warning", "⚠"), ("error", "✗")],
)
@pytest.mark.asyncio
async def test_gutter_block_glyph_matches_across_color_and_no_color(
    kind: str, glyph: str, mock_app_factory: Callable[[Widget], App[None]]
) -> None:
    """The GutterBlock leading_glyph is identical between color + NO_COLOR."""
    env_no_color = dict(os.environ, NO_COLOR="1")
    env_color = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}

    with mock.patch.dict(os.environ, env_color, clear=True):
        w_color = NoticeWidget(text="Body", kind=kind)
    with mock.patch.dict(os.environ, env_no_color, clear=True):
        w_mono = NoticeWidget(text="Body", kind=kind)

    async with mock_app_factory(w_color).run_test() as pilot:
        await pilot.pause()
        color_glyph = w_color.query_one(GutterBlock).leading_glyph
    async with mock_app_factory(w_mono).run_test() as pilot:
        await pilot.pause()
        mono_glyph = w_mono.query_one(GutterBlock).leading_glyph

    assert color_glyph == mono_glyph == glyph


def test_update_text_replaces_body_in_place() -> None:
    """`update_text` replaces the raw body; the rail + glyph are unaffected.

    `event_adapter._handle_notice` calls this on repeat dispatches with
    the same ``notice_id`` (drop rate-limit count updates — spec-008
    §Drop rate limit). The body text updates; the widget stays mounted.
    """
    widget = NoticeWidget(text="Original", kind="warning")
    assert widget.text == "Original"
    widget.update_text("Rate limit approaching (94/100)")
    assert widget.text == "Rate limit approaching (94/100)"
    # Kind (and thus rail/glyph) unaffected.
    assert widget.kind == "warning"


def test_kind_property_preserved() -> None:
    """`.kind` still exposes the stored severity."""
    for kind in ("info", "warning", "error"):
        assert NoticeWidget(text="x", kind=kind).kind == kind


@pytest.mark.asyncio
async def test_render_under_pilot(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Widget mounts and the body renders under Pilot."""
    widget = NoticeWidget(text="session resumed", kind="info")
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        # The GutterBlock is present and carries the ℹ glyph.
        block = widget.query_one(GutterBlock)
        assert block.leading_glyph == "ℹ"
        # The body Static holds the message text (no glyph prefix).
        assert widget.text == "session resumed"
