"""bug-031 Test — `WorkflowNodeRow` renders a rotating spinner while running.

Session ``ses_af5980ab`` had a 90.4s dead zone on the ``deduper``
node with a static ``▸`` glyph in the row title — indistinguishable
from a hang. Root cause: the pre-bug-031 ``WorkflowNodeRow`` had no
periodic tick timer.

This test mounts a bare ``WorkflowNodeRow`` in the ``running``
state, waits ~0.24 seconds wall-clock (3 spinner frames at the
0.08s interval), and asserts the row's title glyph changed during
the wait. If the spinner regresses to a static glyph, the title
stays byte-identical across the wait and the test fails.

The three-frame wall-clock wait is unavoidable — the failure mode is
"static glyph forever" and the fix is a wall-clock timer that
rotates frames. A monotonic-clock mock would defeat the regression
signal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cli.chat.spinner import SPINNER_FRAMES as _SPINNER_FRAMES
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


@pytest.mark.asyncio
async def test_node_row_spinner_rotates_while_running(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """The row's collapsed-title glyph rotates during the running state."""
    row = WorkflowNodeRow(node_name="deduper")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("running")
        # Wait past the first spinner tick so the title picks up the
        # spinner frame (initial title in ``__init__`` is a static
        # placeholder set before the tick fires; qol-075 dropped the
        # per-widget ``STATE_GLYPHS`` map — the placeholder is now
        # hard-coded to ``⠋`` at the __init__ site).
        await pilot.pause(0.1)

        title_before = row.title
        # 0.24s ≥ 3 spinner ticks at 0.08s. Even under Pilot's
        # sometimes-uneven scheduler this observes a glyph rotation.
        await pilot.pause(0.24)
        title_after = row.title

        assert title_before != title_after, (
            f"WorkflowNodeRow title did not change during a 0.24s "
            f"wall-clock wait — the spinner interval regressed to "
            f"the pre-bug-031 static glyph. "
            f"before={title_before!r} after={title_after!r}"
        )

        # Both titles carry a spinner frame (not the static ``▸``).
        assert any(f in title_before for f in _SPINNER_FRAMES), (
            f"title_before {title_before!r} carried no spinner frame"
        )
        assert any(f in title_after for f in _SPINNER_FRAMES), (
            f"title_after {title_after!r} carried no spinner frame"
        )


@pytest.mark.asyncio
async def test_node_row_spinner_stops_at_terminal_state(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """Terminal-state transition stops the spinner interval.

    Guards the "widget keeps waking every 80ms forever after the
    node terminates" regression — ``watch_state`` stops the handle
    on a non-animated transition.
    """
    row = WorkflowNodeRow(node_name="deduper")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("running")
        await pilot.pause(0.1)

        # Transition to terminal.
        row.set_state("success", details="42 rows")
        await pilot.pause()

        # Static glyph (``✓``) — no spinner frame in the title.
        assert "✓" in row.title
        assert not any(f in row.title for f in _SPINNER_FRAMES), (
            f"terminal-state row {row.title!r} still carries a "
            f"spinner frame — ``watch_state`` didn't stop the timer."
        )
        # Handle cleared.
        assert row._spinner_handle is None


@pytest.mark.asyncio
async def test_spinner_stops_on_node_failed(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    """bug-063 VERIFY — ``node.failed`` transitions stop the spinner.

    ``failed`` is a terminal state; ``watch_state`` clears the
    spinner interval when the new state leaves ``_ANIMATED_STATES``.
    Guards the "spinner keeps waking on a failed row" regression.
    NO new production code — this test verifies existing behavior.
    """
    row = WorkflowNodeRow(node_name="identify_missing")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        row.set_state("running")
        await pilot.pause(0.1)

        # Transition to failed — the terminal failure kind.
        row.set_state("failed", error_preview="boom")
        await pilot.pause()

        assert row.state == "failed"
        # Static ``✗`` glyph — no spinner frame.
        assert "✗" in row.title
        assert not any(f in row.title for f in _SPINNER_FRAMES), (
            f"failed-state row {row.title!r} still carries a "
            f"spinner frame — ``watch_state`` didn't stop the timer."
        )
        # Handle cleared.
        assert row._spinner_handle is None
