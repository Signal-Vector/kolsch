"""Pilot tests for :class:`UserChatbox` (post spec-078 migration).

Modularity rule (spec-textual-chat-ui Design Principles #2): the
widget is tested in isolation via ``mock_app_factory`` from the
package conftest — no other widget, no theme.tcss, no app-level
wiring is required for the assertions below to hold.

Post spec-078, the widget composes a :class:`GutterBlock` rather
than rendering as a bordered ``Static``. Assertions reflect the new
shape:

1. Constructor still takes ``(text: str, timestamp: datetime)`` and
   the widget mounts cleanly.
2. The identity header inside the body renders ``"You · HH:MM"`` —
   minute precision, zero-padded. (Pre-migration this was in
   ``border_title`` at HH:MM:SS precision.)
3. The body's raw-user-text ``Static`` is constructed with
   ``markup=False`` so backticks and Rich markup brackets survive
   verbatim — the same contract the pre-migration widget honored.
4. The widget composes a :class:`GutterBlock` with
   ``rail_color="$user-border"`` and no leading glyph — the
   spec-078 target render.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Static

from cli.chat.widgets._gutter_block import GutterBlock
from cli.chat.widgets.user_chatbox import UserChatbox

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


@pytest.mark.asyncio
async def test_user_chatbox_mounts_with_identity_header(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Identity header renders ``"You · HH:MM"`` from the timestamp."""
    ts = datetime(2026, 7, 4, 14, 30, 45, tzinfo=UTC)
    widget = UserChatbox("hello world", ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        header = widget.query_one(".uc-header", Static)
        assert header.content == "You · 14:30"


@pytest.mark.asyncio
async def test_user_chatbox_renders_literal_text_with_backticks(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Backticks (markdown code) and brackets (Rich markup) survive
    as literal characters — the body ``Static`` is constructed with
    ``markup=False``, mirroring the pre-migration contract.
    """
    ts = datetime(2026, 7, 4, 9, 0, 0, tzinfo=UTC)
    raw = "check `foo.py` and [bold]not bold[/bold]"
    widget = UserChatbox(raw, ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        body = widget.query_one(".uc-body", Static)
        # The body Static keeps the raw string verbatim.
        assert body.content == raw


@pytest.mark.asyncio
async def test_user_chatbox_composes_gutter_block(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The widget composes exactly one :class:`GutterBlock` with
    ``rail_color="$user-border"`` and no leading glyph.
    """
    ts = datetime(2026, 7, 4, 0, 0, 0, tzinfo=UTC)
    widget = UserChatbox("x", ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        blocks = list(widget.query(GutterBlock))
        assert len(blocks) == 1
        block = blocks[0]
        assert block.rail_color == "$user-border"
        assert block.leading_glyph is None


@pytest.mark.asyncio
async def test_user_chatbox_header_pads_single_digit_time(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Identity header uses HH:MM with zero-padding — never ``9:5``."""
    ts = datetime(2026, 1, 1, 9, 5, 1, tzinfo=UTC)
    widget = UserChatbox("morning", ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        header = widget.query_one(".uc-header", Static)
        assert header.content == "You · 09:05"


@pytest.mark.asyncio
async def test_user_chatbox_no_round_border(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Post-migration the widget must not declare ``border: round``
    anywhere in its DEFAULT_CSS — the visual border is delegated to
    the GutterBlock rail.
    """
    ts = datetime(2026, 7, 4, 0, 0, 0, tzinfo=UTC)
    widget = UserChatbox("x", ts)
    app = mock_app_factory(widget)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert "border: round" not in UserChatbox.DEFAULT_CSS


@pytest.mark.asyncio
async def test_public_text_and_timestamp_read_back(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """``text`` and ``timestamp`` properties read back the constructor args."""
    ts = datetime(2026, 4, 1, 12, 34, 56, tzinfo=UTC)
    widget = UserChatbox("readback", ts)
    async with mock_app_factory(widget).run_test() as pilot:
        await pilot.pause()
        assert widget.text == "readback"
        assert widget.timestamp == ts
