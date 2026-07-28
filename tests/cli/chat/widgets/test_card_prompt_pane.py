"""spec-007 Test 6 — `CardPromptPane` widget renders.

Two passes:

1. Mount `CardPromptPane(question="OK?", options=["y","n"])`.
   Assert three `Static` children: question, options row, cursor.
2. Options row renders as `[y]  [n]`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.widgets import Static

from cli.chat.widgets.card_prompt_pane import CardPromptPane

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


@pytest.mark.asyncio
async def test_pane_mounts_three_statics(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    pane = CardPromptPane(question="OK?", options=["y", "n"])
    app = mock_app_factory(pane)
    async with app.run_test() as pilot:
        await pilot.pause()
        statics = list(pane.query(Static))
        assert len(statics) == 3
        # First: question. Second: options. Third: cursor.
        assert "OK?" in str(statics[0].render())
        assert "›" in str(statics[2].render())  # noqa: RUF001 — asserts the literal glyph rendered


@pytest.mark.asyncio
async def test_options_row_renders_bracketed(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    pane = CardPromptPane(question="OK?", options=["y", "n"])
    app = mock_app_factory(pane)
    async with app.run_test() as pilot:
        await pilot.pause()
        statics = list(pane.query(Static))
        options_render = str(statics[1].render())
        assert "[y]" in options_render
        assert "[n]" in options_render
