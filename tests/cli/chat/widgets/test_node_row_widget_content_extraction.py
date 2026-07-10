"""bug-034 regression — content-extraction helpers still ship.

bug-036d: the row is now a ``ChatMountSink``, so the tests call
``mount_*`` on the row directly rather than going through
``apply_sub_agent_event``. The extraction helpers
(``_extract_content_text`` / ``_format_content_dict``) still live
on the row module and remain the transformation pipeline for
callers who need to extract text from an event ``content`` dict
before passing it to a sink method.

Sub-tests:

1. ``set_output`` renders bash-shape output with real newlines.
2. ``mount_assistant_message`` (called with the extracted text)
   lands an ``AssistantProse`` inside ``Contents``.
3. Extracted text from a structured-output dict is non-empty (so
   the caller wouldn't skip the mount).
4. Tool result body renders with real newlines via
   ``_format_content_dict``.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Collapsible

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.workflow_node_row import (
    WorkflowNodeRow,
    _extract_content_text,
    _format_content_dict,
)

_AppFactory = Callable[[Widget], App[None]]


@pytest.mark.asyncio
async def test_set_output_bash_shape_renders_real_newlines(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #1 — bash-shape output renders with actual newlines."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output({"stdout": "a\nb\nc", "exit_code": 0})
        await pilot.pause()

        assert row._output_widget is not None
        content = str(row._output_widget.content)
        assert "a\nb\nc" in content
        assert "\\n" not in content
        assert "stdout:" in content
        assert "exit_code: 0" in content


def test_extract_content_text_returns_readable_string_for_dict() -> None:
    """bug-034 helper — non-``text``-keyed dicts still yield readable output.

    bug-036d: the sink caller (adapter handler) extracts text
    BEFORE calling the sink method. The helper's fallback chain
    remains the transformation stage that turns a bound-inputs
    dict into something ``mount_user_message`` / ``mount_assistant_message``
    can render.
    """
    result = _extract_content_text(
        {"top_10_requirements": [{"req": "python"}]},
    )
    assert result
    assert "top_10_requirements" in result or "python" in result


@pytest.mark.asyncio
async def test_assistant_prose_mount_lands_inside_contents(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-036d — ``mount_assistant_message`` mounts inside Contents."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("the answer is 42")
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        prose = list(contents.query(AssistantProse))
        assert prose


@pytest.mark.asyncio
async def test_tool_call_body_renders_bash_shape_with_real_newlines(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-034 — the shared helper renders tool.result bash-shape bodies."""
    body = _format_content_dict({"stdout": "hello\nworld", "exit_code": 0})
    assert "hello\nworld" in body
    assert "\\n" not in body
    assert "stdout:" in body
    assert "exit_code: 0" in body


@pytest.mark.asyncio
async def test_tool_call_block_mounts_via_sink(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-036d — ``mount_tool_call`` mounts a ``ToolCallBlock`` in Contents."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_tool_call(
            "bash",
            args_str='{"command":"ls -la"}',
            body="exit_code: 0\n\nstdout:\nfoo\nbar",
        )
        await pilot.pause()

        contents = row.query_one(Collapsible.Contents)
        blocks = list(contents.query(ToolCallBlock))
        assert blocks


def test_helpers_are_module_private() -> None:
    """Helpers stay underscore-prefixed; public surface is still just the row."""
    from cli.chat.widgets import workflow_node_row as mod

    assert callable(getattr(mod, "_format_content_dict", None))
    assert callable(getattr(mod, "_extract_content_text", None))
    assert mod.__all__ == ["WorkflowNodeRow"]
