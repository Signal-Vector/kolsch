"""bug-032 regression — ``WorkflowNodeRow.set_output`` mounts the preview.

The workflow executor emits ``WorkflowNodeCompletedEvent`` with a
``payload.output`` dict (bash nodes: ``{stdout, stderr, exit_code}``;
agent nodes: bound outputs). Pre-bug-032 the renderer's
``_on_node_completed`` read a non-existent ``payload['summary']`` and
never touched ``payload.output`` — expanded rows sat empty even
though the completed-node data was on the wire.

Four passes cover the new ``set_output`` surface:

1. Non-empty dict → mounts a `Static` inside the `Collapsible`
   whose content is the pretty-printed JSON.
2. Second call replaces the first mount (idempotent).
3. ``None`` / ``{}`` / ``""`` → no widget mounted.
4. > 500-char output → mounted `Static` content ends in ``…``.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets.workflow_node_row import WorkflowNodeRow

_AppFactory = Callable[[Widget], App[None]]


@pytest.mark.asyncio
async def test_set_output_mounts_static_with_formatted_json(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #4 — non-empty output dict lands as a Static preview."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output({"stdout": "hello", "exit_code": 0})
        await pilot.pause()

        statics = [c for c in row.walk_children() if isinstance(c, Static)]
        # The row-body Static tracked by ``_output_widget`` is the
        # one we care about; other Statics (title chrome) may exist
        # but the tracked handle is the load-bearing assertion.
        assert row._output_widget is not None
        assert row._output_widget in statics
        content = str(row._output_widget.content)
        assert "stdout" in content
        assert "hello" in content


@pytest.mark.asyncio
async def test_set_output_idempotent_replaces_previous(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #3 — second call removes the first mount."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output({"a": 1})
        await pilot.pause()
        first_widget = row._output_widget
        assert first_widget is not None

        row.set_output({"b": 2})
        await pilot.pause()

        # ``_output_widget`` now points at the fresh mount.
        assert row._output_widget is not None
        assert row._output_widget is not first_widget
        # First widget was removed from the tree.
        current_statics = [
            c for c in row.walk_children() if isinstance(c, Static)
        ]
        assert first_widget not in current_statics
        # Content reflects the newer payload, not the older one.
        content = str(row._output_widget.content)
        assert '"b": 2' in content
        assert '"a": 1' not in content


@pytest.mark.asyncio
async def test_set_output_noop_for_empty_or_none(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #2 guard — empty / None output mounts nothing."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output(None)
        row.set_output({})
        row.set_output("")
        await pilot.pause()

        assert row._output_widget is None


@pytest.mark.asyncio
async def test_set_output_truncates_long_content(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #2 truncation — > 500-char preview ends in ``…``."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # A ~600-char stdout blob pretty-prints well past the 500
        # char cap so the truncation branch is guaranteed to fire.
        row.set_output({"stdout": "x" * 600, "exit_code": 0})
        await pilot.pause()

        assert row._output_widget is not None
        content = str(row._output_widget.content)
        assert content.endswith("…")
        assert len(content) <= WorkflowNodeRow._OUTPUT_PREVIEW_MAX_LEN
