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
    mock_app_factory: _AppFactory,
) -> None:
    """AC #4 + bug-037 — bash-shape output creates output/exit sections."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output({"stdout": "hello", "exit_code": 0})
        await pilot.pause()

        # bug-037: bash-shape → dedicated sections. _output_widget
        # points at the output NodeSection.
        from cli.chat.widgets.node_section import NodeSection  # noqa: PLC0415
        assert row._output_widget is not None
        assert isinstance(row._output_widget, NodeSection)
        # The body Static inside the output section carries the stdout.
        statics = [
            c for c in row._output_widget.walk_children()
            if isinstance(c, Static)
        ]
        body_texts = [str(getattr(s, "content", "")) for s in statics]
        assert any("hello" in t for t in body_texts)


@pytest.mark.asyncio
async def test_set_output_idempotent_replaces_previous(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #3 + bug-037 — second bash output call is a no-op (sections stick)."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # bug-037: bash-shape outputs create dedicated sections that
        # are NOT replaced on a second call (the sections are keyed
        # on section identity — a re-fire is a no-op).
        row.set_output({"stdout": "a", "exit_code": 0})
        await pilot.pause()
        first_section = row._output_widget
        assert first_section is not None

        row.set_output({"stdout": "b", "exit_code": 0})
        await pilot.pause()

        # Sections don't replay; the first section remains.
        assert row._output_widget is first_section
        # For non-bash-shape outputs the idempotent-replace semantic
        # still holds.
        row2 = WorkflowNodeRow(node_name="n2")
        app2 = mock_app_factory(row2)
        async with app2.run_test() as pilot2:
            await pilot2.pause()
            row2.set_output({"a": 1})
            await pilot2.pause()
            first_widget = row2._output_widget
            assert first_widget is not None
            row2.set_output({"b": 2})
            await pilot2.pause()
            assert row2._output_widget is not first_widget


@pytest.mark.asyncio
async def test_set_output_noop_for_empty_or_none(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #2 guard — empty / None output mounts nothing."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output(None)
        row.set_output({})
        row.set_output("")
        await pilot.pause()

        assert row._output_widget is None


@pytest.mark.asyncio
async def test_set_output_truncates_long_content(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #2 truncation + bug-037 — 500-char stdout ends in ``…`` inside output section."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # A ~600-char stdout blob past the per-block 500 char cap.
        row.set_output({"stdout": "x" * 600, "exit_code": 0})
        await pilot.pause()

        assert row._output_widget is not None
        statics = [
            c for c in row._output_widget.walk_children()
            if isinstance(c, Static)
        ]
        body_texts = [str(getattr(s, "content", "")) for s in statics]
        assert any(t.endswith("…") for t in body_texts)
