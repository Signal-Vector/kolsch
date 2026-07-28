"""spec-007 Test 4 — `ConfirmRenderer` mount + set.

One pass:

- Mount `ConfirmRenderer`; dispatch `card.set("Are you sure?")`.
  Assert `Static` renders "Are you sure?"; no `CardPromptPane`
  mounted (that lands with spec-014).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from textual.widgets import Static

from cli.chat.widgets.card_body.confirm import ConfirmRenderer
from cli.chat.widgets.card_prompt_pane import CardPromptPane

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


@pytest.mark.asyncio
async def test_set_updates_confirm_text(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    widget = ConfirmRenderer()
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        widget.apply_event(
            _MockEvent(
                "kolsch.card.set",
                {"content": "Are you sure?"},
            )
        )
        await pilot.pause()
        text = widget.query_one(Static)
        assert "Are you sure?" in str(text.render())
        # No CardPromptPane mounted — that's spec-014.
        panes = list(widget.query(CardPromptPane))
        assert panes == []
