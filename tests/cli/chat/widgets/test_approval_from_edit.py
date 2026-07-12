"""Pilot tests for ApprovalFromEdit.

Each test wraps the widget in a minimal host `App` via the
`mock_app_factory` fixture (see `tests/cli/chat/conftest.py`) and drives
it with `Pilot`. No CSS beyond the widget's `DEFAULT_CSS` is loaded —
this keeps the widget honest as a modular unit.
"""

from __future__ import annotations

import pytest
from textual.widgets import Input

from cli.chat.widgets.approval_from_edit import ApprovalFromEdit


@pytest.mark.asyncio
async def test_mounts_with_suggested_from_and_input_has_focus(
    mock_app_factory,
) -> None:
    widget = ApprovalFromEdit(suggested_from="agent:*")
    app = mock_app_factory(widget)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = widget.query_one(Input)
        assert input_widget.value == "agent:*"
        assert input_widget.has_focus
        assert input_widget.id == "from-scope"


@pytest.mark.asyncio
async def test_enter_posts_confirmed_with_current_value(
    mock_app_factory,
) -> None:
    widget = ApprovalFromEdit(suggested_from="agent:*")
    app = mock_app_factory(widget)

    received: list[ApprovalFromEdit.Confirmed] = []

    async with app.run_test() as pilot:
        await pilot.pause()

        # Intercept Confirmed messages posted by the widget.
        original = widget.post_message

        def _capture(message):  # type: ignore[no-untyped-def]
            if isinstance(message, ApprovalFromEdit.Confirmed):
                received.append(message)
            return original(message)

        widget.post_message = _capture  # type: ignore[method-assign]

        await pilot.press("enter")
        await pilot.pause()

    assert len(received) == 1
    assert received[0].from_scope == "agent:*"


@pytest.mark.asyncio
async def test_enter_confirms_edited_value(mock_app_factory) -> None:
    widget = ApprovalFromEdit(suggested_from="")
    app = mock_app_factory(widget)

    received: list[ApprovalFromEdit.Confirmed] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        original = widget.post_message

        def _capture(message):  # type: ignore[no-untyped-def]
            if isinstance(message, ApprovalFromEdit.Confirmed):
                received.append(message)
            return original(message)

        widget.post_message = _capture  # type: ignore[method-assign]

        # Type a value into the focused Input, then submit.
        await pilot.press("a", "b", "c")
        await pilot.press("enter")
        await pilot.pause()

    assert len(received) == 1
    assert received[0].from_scope == "abc"


@pytest.mark.asyncio
async def test_escape_posts_cancelled(mock_app_factory) -> None:
    widget = ApprovalFromEdit(suggested_from="agent:*")
    app = mock_app_factory(widget)

    received: list[ApprovalFromEdit.Cancelled] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        original = widget.post_message

        def _capture(message):  # type: ignore[no-untyped-def]
            if isinstance(message, ApprovalFromEdit.Cancelled):
                received.append(message)
            return original(message)

        widget.post_message = _capture  # type: ignore[method-assign]

        await pilot.press("escape")
        await pilot.pause()

    assert len(received) == 1
