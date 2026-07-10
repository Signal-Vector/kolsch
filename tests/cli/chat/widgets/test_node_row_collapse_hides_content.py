"""bug-035 regression — collapsing a `WorkflowNodeRow` hides its body.

Sink methods (bug-036d) still route mounts through
``_mount_in_contents`` so the mount-tree invariant survives the
refactor.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow


_AppFactory = Callable[[Widget], App[None]]


@pytest.fixture
def fake_app_factory() -> _AppFactory:
    def factory(child: Widget) -> App[None]:
        class _FakeApp(App[None]):
            def compose(self):
                yield child
        return _FakeApp()
    return factory


def _contents_of(row: WorkflowNodeRow) -> Collapsible.Contents:
    """Return the Collapsible.Contents container inside ``row``."""
    return row.query_one(Collapsible.Contents)


@pytest.mark.asyncio
async def test_set_output_mounts_inside_contents(
    fake_app_factory: _AppFactory,
) -> None:
    """``set_output`` mounts inside the Contents slot."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output({"stdout": "hello", "exit_code": 0})
        await pilot.pause()

        contents = _contents_of(row)
        assert row._output_widget is not None
        assert row._output_widget in list(contents.walk_children())


@pytest.mark.asyncio
async def test_assistant_message_mounts_inside_contents(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-036d sink — ``mount_assistant_message`` lands inside Contents."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("the answer is 42")
        await pilot.pause()

        contents = _contents_of(row)
        prose_widgets = list(contents.query(AssistantProse))
        assert prose_widgets


@pytest.mark.asyncio
async def test_tool_call_mounts_inside_contents(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-036d sink — ``mount_tool_call`` lands inside Contents."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_tool_call(
            "bash",
            args_str='{"command":"ls -la"}',
            body="",
        )
        await pilot.pause()

        contents = _contents_of(row)
        tool_blocks = list(contents.query(ToolCallBlock))
        assert tool_blocks


@pytest.mark.asyncio
async def test_collapse_hides_body_via_css_class(
    fake_app_factory: _AppFactory,
) -> None:
    """Collapsing the row applies the ``.-collapsed`` class."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("hello")
        await pilot.pause()

        contents = _contents_of(row)
        assert list(contents.query(AssistantProse))

        row.collapsed = True
        await pilot.pause()

        assert row.has_class("-collapsed")
        assert list(contents.query(AssistantProse))
