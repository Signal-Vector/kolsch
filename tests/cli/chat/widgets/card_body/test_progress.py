"""spec-007 Test 3 — `ProgressRenderer` mount + update.

Two passes:

1. Mount `ProgressRenderer`; dispatch
   `card.set_progress(current=5, total=10, message="halfway")`.
   Assert `Static` shows "halfway", `ProgressBar.progress == 5`,
   `.total == 10`.
2. Dispatch with `total=0` (indeterminate). Bar renders in
   indeterminate mode (`total is None`); no crash.
"""

from __future__ import annotations

from typing import Any

import pytest
from textual.widgets import ProgressBar, Static

from cli.chat.widgets.card_body.progress import ProgressRenderer


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


@pytest.mark.asyncio
async def test_set_progress_updates_message_and_bar(mock_app_factory) -> None:
    widget = ProgressRenderer()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        widget.apply_event(
            _MockEvent(
                "kolsch.card.set_progress",
                {"current": 5, "total": 10, "message": "halfway"},
            )
        )
        await pilot.pause()
        # The message Static (first child) rendered "halfway".
        message = widget.query_one(Static)
        assert "halfway" in str(message.render())
        bar = widget.query_one(ProgressBar)
        assert bar.progress == 5.0
        assert bar.total == 10.0


@pytest.mark.asyncio
async def test_indeterminate_when_total_zero(mock_app_factory) -> None:
    """`total=0` → indeterminate: bar's total is `None` (spinner mode)."""
    widget = ProgressRenderer()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        widget.apply_event(
            _MockEvent(
                "kolsch.card.set_progress",
                {"current": 3, "total": 0, "message": "indeterminate"},
            )
        )
        await pilot.pause()
        bar = widget.query_one(ProgressBar)
        # Textual's ProgressBar renders a spinner when `total` is None.
        assert bar.total is None
