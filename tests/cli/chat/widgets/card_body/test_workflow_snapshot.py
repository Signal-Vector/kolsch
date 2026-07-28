"""spec-013 Test 6 — `WorkflowRenderer.apply_snapshot` no-op.

One pass:

- `WorkflowRenderer().apply_snapshot("content")` returns `None`
  without touching internal state or raising.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cli.chat.widgets.card_body.workflow import WorkflowRenderer

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.widget import Widget


@pytest.mark.asyncio
async def test_apply_snapshot_is_noop(
    mock_app_factory: Callable[[Widget], App[None]],
) -> None:
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Snapshot with arbitrary content — must not raise.
        result = renderer.apply_snapshot("arbitrary markdown content")  # type: ignore[func-returns-value]  # asserting no-op returns None
        assert result is None
        # State stays fresh: no run anchored, no nodes mounted, no
        # footer content populated.
        assert renderer._run_id is None
        assert renderer._node_rows == {}
        # A follow-up snapshot with an empty string is equally silent.
        assert renderer.apply_snapshot("") is None  # type: ignore[func-returns-value]  # asserting no-op returns None
