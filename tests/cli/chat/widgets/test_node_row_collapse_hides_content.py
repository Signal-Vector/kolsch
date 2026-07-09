"""bug-035 regression — collapsing a `WorkflowNodeRow` hides its body.

Before bug-035, ``_mount_sub_agent_widget`` and ``set_output`` called
``self.mount(widget)`` directly on the ``Collapsible``. Textual's
``compose_add_child`` only wires the *context-manager* compose syntax
to the internal ``Contents`` container — a bare ``self.mount(child)``
on an already-composed Collapsible mounts as a **sibling** of the
``CollapsibleTitle`` and ``Contents``. Only ``Contents`` responds to
the ``.-collapsed > Contents { display: none }`` CSS, so sibling
mounts stayed visible regardless of the row's collapsed state.

Post-fix (``_mount_in_contents``): every widget lands inside the
``Collapsible.Contents`` slot. This test asserts the invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId


_AppFactory = Callable[[Widget], App[None]]


def _mk_event(event_type: EventType, data: dict[str, Any]) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        parent_event_id=None,
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=event_type,
        data=data,
        actor=Actor(kind="agent", id="ag_x"),
        created_at=datetime.now(tz=UTC),
    )


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
    """bug-035 AC — ``set_output`` mounts inside the Contents slot.

    Before the fix, the ``Static`` landed as a sibling of Contents,
    which meant collapsing the row didn't hide it. Now every mount
    goes into Contents so the CSS ``.-collapsed > Contents`` rule
    controls visibility.
    """
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        row.set_output({"stdout": "hello", "exit_code": 0})
        await pilot.pause()

        contents = _contents_of(row)
        statics = list(contents.query(Static))
        # The tracked _output_widget MUST live inside Contents.
        assert row._output_widget is not None
        assert row._output_widget in list(contents.walk_children())


@pytest.mark.asyncio
async def test_agent_message_widget_mounts_inside_contents(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-035 AC — AGENT_MESSAGE widget mounts inside Contents."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        event = _mk_event(
            EventType.AGENT_MESSAGE,
            data={"content": {"text": "the answer is 42"}, "exchange_id": "ex_1"},
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        contents = _contents_of(row)
        prose_widgets = list(contents.query(AssistantProse))
        assert prose_widgets, (
            "AssistantProse should mount inside Contents, not as a "
            "sibling of CollapsibleTitle + Contents"
        )


@pytest.mark.asyncio
async def test_tool_call_widget_mounts_inside_contents(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-035 AC — TOOL_CALLED widget mounts inside Contents."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        event = _mk_event(
            EventType.TOOL_CALLED,
            data={
                "tool_name": "bash",
                "arguments": {"command": "ls -la"},
                "tool_call_id": "tc_1",
            },
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        contents = _contents_of(row)
        tool_blocks = list(contents.query(ToolCallBlock))
        assert tool_blocks, (
            "ToolCallBlock should mount inside Contents, not as a "
            "sibling of CollapsibleTitle + Contents"
        )


@pytest.mark.asyncio
async def test_collapse_hides_body_via_css_class(
    fake_app_factory: _AppFactory,
) -> None:
    """bug-035 AC — collapsing the row applies the ``.-collapsed`` class.

    Textual's Collapsible CSS rule
    ``.-collapsed > Contents { display: none }`` is what actually hides
    the body. Since every widget now lives inside Contents (bug-035),
    this class toggle governs their visibility.
    """
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Mount an AGENT_MESSAGE while expanded.
        event = _mk_event(
            EventType.AGENT_MESSAGE,
            data={"content": {"text": "hello"}, "exchange_id": "ex_1"},
        )
        row.apply_sub_agent_event(event)
        await pilot.pause()

        contents = _contents_of(row)
        assert list(contents.query(AssistantProse)), "widget not mounted"

        # Now collapse the row.
        row.collapsed = True
        await pilot.pause()

        # Collapsible applies the ``-collapsed`` CSS class on collapse;
        # the CSS rule ``.-collapsed > Contents { display: none }``
        # hides Contents — the ONLY container widgets now live in.
        assert row.has_class("-collapsed"), (
            "Collapsible should carry the -collapsed CSS class when "
            "collapsed=True"
        )
        # Widgets still exist in the tree (spec-021 buffer preserves
        # them across collapse cycles); they're just visually hidden.
        assert list(contents.query(AssistantProse)), (
            "widget should still exist in Contents after collapse "
            "(hidden via CSS, not removed)"
        )
