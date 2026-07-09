"""bug-034 regression — `WorkflowNodeRow` widget content extraction.

User ran a workflow via chat and reported 3 rendering symptoms:

1. `set_output` truncated bash-node output as JSON-escaped `\\n`
   sequences chopped mid-string.
2. `_mount_sub_agent_widget` mounted UserChatbox / AssistantProse
   with empty content when `data.content` was a workflow IOSpec
   input dict (no `text` key).
3. `ToolCallBlock` mounted with hardcoded empty `args_str` and
   never surfaced `data.arguments` or `data.result`.

Five passes cover the surface:

1. `test_set_output_bash_shape_renders_real_newlines` — bash-shape
   output renders with actual newline characters, not `\\n` escapes.
2. `test_user_chatbox_falls_back_to_content_dict` — USER_MESSAGE
   with `data.content = {"input": "hi"}` mounts a non-empty
   UserChatbox mentioning the dict content.
3. `test_assistant_prose_falls_back_to_content_dict` —
   AGENT_MESSAGE with structured-output dict content mounts an
   AssistantProse with meaningful content.
4. `test_tool_call_block_populates_args_from_arguments` —
   TOOL_CALLED with `data.arguments = {"command": "ls -la"}`
   mounts a ToolCallBlock whose args string contains the payload.
5. `test_tool_call_block_populates_body_from_result` — TOOL_RESULT
   with `data.result = {stdout: ..., exit_code: 0}` renders the
   body with real newlines via the shared bash-aware helper.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Static

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.user_chatbox import UserChatbox
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

_AppFactory = Callable[[Widget], App[None]]


def _mk_event(event_type: EventType, data: dict[str, Any]) -> Event:
    """Build a well-formed session Event of the requested type.

    Mirrors the shape sub-agent events land with on the wire — the
    dedupe key path in ``WorkflowNodeRow`` reads ``event_id``, so
    each call generates a fresh id.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=event_type,
        data=data,
        actor=Actor(kind="system"),
        created_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_set_output_bash_shape_renders_real_newlines(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #1 — bash-shape output renders with actual newlines.

    Symptom 1 fix: `set_output({stdout: "a\\nb\\nc", exit_code: 0})`
    used to JSON-encode the stdout as `"a\\nb\\nc"` (escaped) and
    truncate mid-string. Now it renders the stdout as a labeled
    block with real ``\\n`` characters so the body reads as
    multi-line text.
    """
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output({"stdout": "a\nb\nc", "exit_code": 0})
        await pilot.pause()

        assert row._output_widget is not None
        content = str(row._output_widget.content)
        # Real newlines, not JSON-escaped `\n`.
        assert "a\nb\nc" in content
        assert "\\n" not in content
        # Labeled blocks so a reader can tell stdout apart from
        # exit_code without hunting.
        assert "stdout:" in content
        assert "exit_code: 0" in content


@pytest.mark.asyncio
async def test_user_message_does_not_mount_a_widget(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-035 — USER_MESSAGE inside a workflow node is skipped.

    The scheduler's node-invocation payload isn't semantically a user
    turn; rendering ``UserChatbox`` ("You: ...") for it was
    cluttering the row body. Skipping the mount lets the row focus on
    the agent's response + tool calls, which carry the actual
    narrative. The turn counter still increments (see
    ``apply_sub_agent_event``) so the collapsed title reflects
    activity.
    """
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        event = _mk_event(
            EventType.USER_MESSAGE,
            data={"content": {"input": "hi"}},
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        # No UserChatbox mounted for USER_MESSAGE in workflow rows.
        boxes = list(row.query(UserChatbox))
        assert not boxes, (
            "USER_MESSAGE should NOT mount UserChatbox inside a "
            "workflow node row (bug-035 design)"
        )
        # Turn counter still bumps so the title stays informative.
        assert row._sub_agent_turn_count == 1


@pytest.mark.asyncio
async def test_assistant_prose_falls_back_to_content_dict(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #3 — AGENT_MESSAGE with structured outputs mounts non-empty.

    Symptom 2 fix (paired with the USER_MESSAGE case). The
    `synthesize` node in workflows emits
    `data.content = {"top_10_requirements": [...]}`; pre-fix that
    became an empty AssistantProse. Post-fix the dict renders
    (via ``_format_content_dict``) into the widget's document.
    """
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        event = _mk_event(
            EventType.AGENT_MESSAGE,
            data={
                "content": {
                    "top_10_requirements": [{"req": "python"}],
                },
            },
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        prose = list(row.query(AssistantProse))
        assert prose, "AssistantProse not mounted for AGENT_MESSAGE"
        # The Markdown widget stores the source in its document.
        # Assert the fallback rendering carried the payload — the
        # AssistantProse constructor forwards its ``text`` arg to
        # ``Markdown.__init__`` which stores it as the initial
        # ``_markdown`` attribute.
        source = getattr(prose[0], "_markdown", "") or ""
        assert "top_10_requirements" in source or "python" in source


@pytest.mark.asyncio
async def test_tool_call_block_populates_args_from_arguments(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #4 — TOOL_CALLED surfaces `data.arguments` into the block title.

    Symptom 3 fix: `args_str` was hardcoded to `""` so the block
    rendered as ``● bash()`` with no invocation visible. Post-fix
    the compact JSON of `data.arguments` populates ``args_str``,
    truncated for the title.
    """
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        event = _mk_event(
            EventType.TOOL_CALLED,
            data={
                "tool_name": "bash",
                "tool_call_id": "call-1",
                "arguments": {"command": "ls -la"},
            },
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        blocks = list(row.query(ToolCallBlock))
        assert blocks, "ToolCallBlock not mounted for TOOL_CALLED"
        block = blocks[0]
        # ``_args`` holds the constructor-time args string.
        args_str = block._args
        assert args_str, "args_str is empty — Symptom 3 unfixed"
        assert "command" in args_str or "ls" in args_str


@pytest.mark.asyncio
async def test_tool_call_block_populates_body_from_result(
    fake_app_factory: _AppFactory,
) -> None:
    """AC #5 — TOOL_RESULT surfaces `data.result` into the block body.

    Symptom 3's second half: `data.result` never made it into the
    body. Post-fix the result flows through ``_format_content_dict``
    so a bash-shape result renders with real newlines under labeled
    stdout / exit_code blocks — same rendering as ``set_output``.
    """
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        event = _mk_event(
            EventType.TOOL_RESULT,
            data={
                "tool_name": "bash",
                "tool_call_id": "call-1",
                "result": {"stdout": "hello\nworld", "exit_code": 0},
            },
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        blocks = list(row.query(ToolCallBlock))
        assert blocks, "ToolCallBlock not mounted for TOOL_RESULT"
        body = blocks[0]._body_text
        # Real newlines in the rendered body — the fix's whole point.
        assert "hello\nworld" in body
        assert "\\n" not in body
        # Labeled blocks so the reader knows which stream is which.
        assert "stdout:" in body
        assert "exit_code: 0" in body


def test_helpers_are_module_private() -> None:
    """AC #8 spirit-check — helpers are underscore-prefixed.

    ``_format_content_dict`` + ``_extract_content_text`` are module-
    scope but private (leading underscore). The public API surface
    is still just ``WorkflowNodeRow`` per the module's ``__all__``.
    """
    from cli.chat.widgets import workflow_node_row as mod

    # Helpers exist and are private.
    assert callable(getattr(mod, "_format_content_dict", None))
    assert callable(getattr(mod, "_extract_content_text", None))
    # Public surface unchanged.
    assert mod.__all__ == ["WorkflowNodeRow"]
