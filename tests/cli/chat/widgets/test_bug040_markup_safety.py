"""bug-040 regression — dynamic-content ``Static`` widgets must not
parse Textual markup.

The live-app crash was ``textual.markup.MarkupError`` when a URL-
encoded string ending in ``"}]}\n`` (a JSON blob nested inside a
larger structure — think signed URL / auth-token content) landed in
a ``Static`` that defaulted to ``markup=True``. Textual's markup
parser saw ``[<url-encoded-content>``, tried to parse it as a
color/style tag, and raised.

The fix disables markup on every ``Static`` that renders content
sourced from users, tools, or the workflow executor. These sub-tests
lock in the fix: each one feeds bracket-heavy content through the
public ``WorkflowNodeRow`` / ``ToolCallBlock`` surface and asserts
that mounting succeeds instead of blowing up.
"""

from __future__ import annotations

from typing import Callable

import pytest
from textual.app import App
from textual.widget import Widget

from cli.chat.widgets.card_prompt_pane import CardPromptPane
from cli.chat.widgets.notice import NoticeWidget
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow

_AppFactory = Callable[[Widget], App[None]]


# The crash sample from the live session — URL-encoded content that
# ends in the exact ``"}]}\n`` sequence that tripped the parser.
_URL_ENCODED_BRACKET_TAIL = (
    "%2FZRrhp7ghYJ26c1fhbGDYx%2FE4ZaNjXG4vKh3zCC%2B6DlUpDz8"
    "%2BK6SVW2a9RCOZlJ9RQ4sFOjtLnTChoH7j4xEb9aaMQVTrTq58e2vPDrKmxx"
    "YBgAmzESJhu%2FDgArCSby0Bpcq6YZQ%3D%3D\"}]}\n"
)


@pytest.mark.asyncio
async def test_workflow_node_set_output_with_bracket_content(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #3 sub-test 1 — non-bash dict payload with ``[brackets]`` mounts."""
    row = WorkflowNodeRow(node_name="n")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Non-bash-shape dict → falls through to the flat Static preview
        # at workflow_node_row.py:421. Pre-fix, the ``[brackets]``
        # in the value would raise MarkupError on mount.
        row.set_output({"data": "value with [brackets] inside"})
        await pilot.pause()

        assert row._output_widget is not None


@pytest.mark.asyncio
async def test_bash_stdout_with_url_encoded_content(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #3 sub-test 2 — the exact live-crash payload mounts cleanly."""
    row = WorkflowNodeRow(node_name="n", node_type="kolsch/nodes/bash")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Bash-shape output → hits _mount_bash_output_sections at
        # workflow_node_row.py:480 (stdout Static) and :499 (exit
        # block Static). Pre-fix, the trailing ``"}]}\n`` in the
        # URL-encoded blob crashed the mount.
        row.set_output(
            {
                "stdout": _URL_ENCODED_BRACKET_TAIL,
                "exit_code": 0,
                "stderr": "",
            }
        )
        await pilot.pause()

        assert row._bash_output_section is not None
        assert row._bash_exit_section is not None


@pytest.mark.asyncio
async def test_tool_call_block_with_bracket_body(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #3 sub-test 3 — ToolCallBlock body + recovery_hint with brackets."""
    block = ToolCallBlock(
        tool_name="fetch",
        args="url=https://example.com",
        body='{"result": "value with [brackets] and %5Bencoded%5D"}',
        recovery_hint="retry with different [params]",
    )
    app = mock_app_factory(block)
    async with app.run_test() as pilot:
        # Pre-fix, mounting the body Static (tool_call_block.py:103)
        # or the hint Static (tool_call_block.py:105) with bracket
        # content would raise MarkupError during compose.
        await pilot.pause()

        assert block.is_mounted


@pytest.mark.asyncio
async def test_bash_command_preview_with_jinja_brackets(
    mock_app_factory: _AppFactory,
) -> None:
    """AC #3 sub-test 4 — set_command_preview with bracket content mounts."""
    row = WorkflowNodeRow(node_name="n", node_type="kolsch/nodes/bash")
    app = mock_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Command preview lands at workflow_node_row.py:463
        # (node-command-preview Static). Jinja template rendering
        # can leave literal ``[...]`` fragments in the command.
        row.set_command_preview(
            "curl 'https://example.com?token=[foo]&auth=[bar]'"
        )
        await pilot.pause()

        assert row._command_widget is not None


@pytest.mark.asyncio
async def test_notice_widget_with_bracket_content(
    mock_app_factory: _AppFactory,
) -> None:
    """Reviewer sweep — ``NoticeWidget`` mounts with bracket-heavy text.

    Executor error content lands via
    ``event_adapter._handle_card_notice`` as ``f"exchange failed — {message}"``
    — the message can contain arbitrary user/tool/executor content
    including the URL-encoded ``"}]}`` tail that tripped the
    live-app crash.
    """
    notice = NoticeWidget(
        text=f"exchange failed — {_URL_ENCODED_BRACKET_TAIL}",
        kind="error",
    )
    app = mock_app_factory(notice)
    async with app.run_test() as pilot:
        # Pre-fix, mounting a NoticeWidget whose text contains
        # ``[`` followed by non-markup content raised MarkupError
        # during the underlying ``Static`` render.
        await pilot.pause()

        assert notice.is_mounted
        assert "[brackets]" not in notice.text  # trivial sanity


@pytest.mark.asyncio
async def test_card_prompt_pane_with_bracket_question(
    mock_app_factory: _AppFactory,
) -> None:
    """Reviewer sweep — ``CardPromptPane`` question row mounts with brackets.

    The prompt question is wired from ``PromptsImpl`` (spec-014)
    and can carry user- or tool-authored content. Bracket content
    in the question would otherwise trip ``MarkupError`` on the
    question-row Static.
    """
    pane = CardPromptPane(
        question="Continue with url [https://x.y/z?tok=[foo]]?",
        options=["y", "n"],
    )
    app = mock_app_factory(pane)
    async with app.run_test() as pilot:
        await pilot.pause()

        assert pane.is_mounted
