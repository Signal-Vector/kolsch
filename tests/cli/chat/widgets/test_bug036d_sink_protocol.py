"""bug-036d — `ChatMountSink` protocol Pilot tests.

The 14 sub-tests cover the row's sink implementation, dedupe,
ordering, terminal-state freeze, unmount cleanup, and the
`_last_assistant_text` gate that consolidates 036a's semantic.

Reviewer #2 flagged sub-tests 7, 9, 11, 12, 14 as regression
guards — nondeterministic drain, closure-leak on unmount,
empty-string boundary, Mockup 6 double-prose, trace-id
mis-routing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Callable
from uuid import uuid4

import pytest
from textual.app import App
from textual.widget import Widget
from textual.widgets import Collapsible

from cli.chat.event_adapter import EventAdapter
from cli.chat.services.mount_sink import ChatMountSink
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.notice import NoticeWidget
from cli.chat.widgets.tool_call_block import ToolCallBlock
from cli.chat.widgets.user_chatbox import UserChatbox
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId


_AppFactory = Callable[[Widget], App[None]]


def _contents_of(row: WorkflowNodeRow) -> Collapsible.Contents:
    return row.query_one(Collapsible.Contents)


# ---------------------------------------------------------------------------
# Sub-test 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_mounts_user_message(fake_app_factory: _AppFactory) -> None:
    """Sink call for USER_MESSAGE mounts a ``UserChatbox`` in Contents."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_user_message("hello")
        await pilot.pause()

        boxes = list(_contents_of(row).query(UserChatbox))
        assert boxes, "UserChatbox should mount inside Contents"


# ---------------------------------------------------------------------------
# Sub-test 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_mounts_assistant_message(
    fake_app_factory: _AppFactory,
) -> None:
    """Sink call for AGENT_MESSAGE / MODEL_RESPONSE mounts an ``AssistantProse``."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_assistant_message("hello world")
        await pilot.pause()

        prose = list(_contents_of(row).query(AssistantProse))
        assert prose


# ---------------------------------------------------------------------------
# Sub-test 3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_mounts_tool_call(fake_app_factory: _AppFactory) -> None:
    """Sink call for TOOL_RESULT mounts a ``ToolCallBlock``."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_tool_call(
            "bash", args_str='{"c":"ls"}', body="exit_code: 0",
        )
        await pilot.pause()

        blocks = list(_contents_of(row).query(ToolCallBlock))
        assert blocks


# ---------------------------------------------------------------------------
# Sub-test 4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_mounts_notice(fake_app_factory: _AppFactory) -> None:
    """Sink call for a notice mounts a ``NoticeWidget`` inside Contents."""
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        await row.mount_notice("nid", "heads up", level="warning")
        await pilot.pause()

        notices = list(_contents_of(row).query(NoticeWidget))
        assert notices


# ---------------------------------------------------------------------------
# Sub-test 5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_last_assistant_text_get_set() -> None:
    """The sink accessors round-trip a string."""
    row = WorkflowNodeRow(node_name="n")
    assert row.last_assistant_text() == ""
    row.set_last_assistant_text("hello")
    assert row.last_assistant_text() == "hello"


# ---------------------------------------------------------------------------
# Sub-test 6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collapsed_row_queues_mounts(
    fake_app_factory: _AppFactory,
) -> None:
    """Collapsed row: nothing lands in Contents until expanded."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert row.collapsed is True

        await row.mount_user_message("A")
        await row.mount_assistant_message("B")
        await pilot.pause()

        contents = _contents_of(row)
        assert not list(contents.query(UserChatbox))
        assert not list(contents.query(AssistantProse))
        assert len(row._pending_mounts) == 2

        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        assert list(contents.query(UserChatbox))
        assert list(contents.query(AssistantProse))


# ---------------------------------------------------------------------------
# Sub-test 7 (Reviewer #2 — Mockup 3 order regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_mount_order_preserved(
    fake_app_factory: _AppFactory,
) -> None:
    """Drain preserves insertion order — UserChatbox then AssistantProse then ToolCallBlock."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_user_message("A", event_id="ev-1")
        await row.mount_assistant_message("B", event_id="ev-2")
        await row.mount_tool_call(
            "bash", args_str="", body="",
            event_id="ev-3",
        )
        await pilot.pause()

        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        # Walk Contents and record the first hit of each type.
        contents = _contents_of(row)
        children = list(contents.walk_children())
        types_in_order = [type(w).__name__ for w in children]

        # The three widget types should appear in the drain order,
        # although Collapsible internals may add other widgets in
        # between. Filter to just the three widget classes.
        wanted = {"UserChatbox", "AssistantProse", "ToolCallBlock"}
        filtered = [t for t in types_in_order if t in wanted]

        assert filtered == ["UserChatbox", "AssistantProse", "ToolCallBlock"], (
            f"Mockup 3 order broken: {filtered}"
        )


# ---------------------------------------------------------------------------
# Sub-test 8
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_state_drops_post_freeze_mounts(
    fake_app_factory: _AppFactory,
) -> None:
    """Post-terminal ``mount_*`` invocations are dropped by the freeze.

    Pre-terminal queued closures survive — a user who expands the
    row after the run finishes still sees the response history
    (spec-021 replay-on-expand invariant). This test guards the
    inverse: a ``mount_*`` invocation that arrives AFTER the freeze
    (e.g., a late sub-agent event racing NODE_COMPLETED) is dropped
    without side effects.
    """
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Pre-terminal: this closure survives the freeze.
        await row.mount_assistant_message("pre", event_id="ev-1")
        await pilot.pause()
        assert row._sub_agent_turn_count == 1

        row.set_state("completed")
        await pilot.pause()
        assert row._buffer_frozen is True

        # Post-terminal: this call is short-circuited.
        await row.mount_assistant_message("post", event_id="ev-2")
        await pilot.pause()

        assert row._sub_agent_turn_count == 1, (
            "post-terminal mount must NOT bump the counter"
        )
        # Expanding replays the pre-terminal closure only.
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        contents = _contents_of(row)
        prose = list(contents.query(AssistantProse))
        assert len(prose) == 1


# ---------------------------------------------------------------------------
# Sub-test 9 (Reviewer #1 — closure-leak guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmount_clears_pending_mounts(
    fake_app_factory: _AppFactory,
) -> None:
    """on_unmount clears pending closures + latches ``_unmounted``."""
    row = WorkflowNodeRow(node_name="n")
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("queued", event_id="ev-1")
        await pilot.pause()
        assert len(row._pending_mounts) == 1

        await row.remove()
        await pilot.pause()

        assert row._pending_mounts == []
        assert row._unmounted is True


# ---------------------------------------------------------------------------
# Sub-test 10
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_output_gate_reads_last_assistant_text(
    fake_app_factory: _AppFactory,
) -> None:
    """Agent node: non-empty last-assistant-text → set_output no-ops."""
    row = WorkflowNodeRow(
        node_name="n", node_type="kolsch/nodes/agent",
    )
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("real response")
        await pilot.pause()

        row.set_output({"summary": "would be a duplicate"})
        await pilot.pause()

        assert row._output_widget is None


# ---------------------------------------------------------------------------
# Sub-test 11 (Reviewer #2 — empty-string boundary)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_output_gate_empty_text_edge(
    fake_app_factory: _AppFactory,
) -> None:
    """Empty ``_last_assistant_text`` → gate does NOT fire.

    Prevents semantic drift from the pre-036d ``_saw_agent_message:
    bool`` shape: setting the string to ``""`` explicitly must
    behave as "nothing rendered" so ``set_output`` still runs.
    """
    row = WorkflowNodeRow(
        node_name="n", node_type="kolsch/nodes/agent",
    )
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Explicit empty — matches the constructor default.
        row.set_last_assistant_text("")

        row.set_output({"text": "..."})
        await pilot.pause()

        assert row._output_widget is not None


# ---------------------------------------------------------------------------
# Sub-test 12 (Reviewer #2 — Mockup 6 double-prose)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transformed_agent_message_mounts_second_prose(
    fake_app_factory: _AppFactory,
) -> None:
    """Two distinct assistant messages → two AssistantProse widgets.

    Dedupe skipped when content differs. Reproduces Mockup 6 —
    one prose from ``model.response``, one from the transformed
    ``agent.message``.
    """
    row = WorkflowNodeRow(node_name="n")
    row.collapsed = False
    app = fake_app_factory(row)
    async with app.run_test() as pilot:
        await pilot.pause()

        await row.mount_assistant_message("raw", event_id="ev-a")
        row.set_last_assistant_text("raw")
        await row.mount_assistant_message("transformed", event_id="ev-b")
        row.set_last_assistant_text("transformed")
        await pilot.pause()

        contents = _contents_of(row)
        prose = list(contents.query(AssistantProse))
        assert len(prose) == 2


# ---------------------------------------------------------------------------
# Sub-test 13
# ---------------------------------------------------------------------------


class _FakeChatApp:
    """Bare-bones sink stub for the adapter — only reads/writes state."""

    def __init__(self) -> None:
        self._last: str = ""
        self.calls: list[str] = []

    async def mount_user_message(
        self, text: str, timestamp: Any = None,
        *, event_id: Any = None,
    ) -> None:
        _ = timestamp, event_id
        self.calls.append(f"user:{text}")

    async def mount_assistant_message(
        self, text: str, *, event_id: Any = None,
    ) -> None:
        _ = event_id
        self.calls.append(f"assistant:{text}")

    async def mount_tool_call(self, *args: Any, **kwargs: Any) -> None:
        _ = args, kwargs
        self.calls.append("tool")

    async def mount_notice(
        self, notice_id: str, text: str,
        *, level: str = "info", event_id: Any = None,
    ) -> None:
        _ = notice_id, text, level, event_id
        self.calls.append(f"notice:{text}")

    async def mount_exchange_failed(self, message: str) -> None:
        self.calls.append(f"failed:{message}")

    async def set_turn_verb(self, verb: str, *, target: str = "") -> None:
        _ = verb, target

    def last_assistant_text(self) -> str:
        return self._last

    def set_last_assistant_text(self, text: str) -> None:
        self._last = text


def _mk_event(
    *, trace_id: str | None = None, data: dict[str, Any] | None = None,
    event_type: EventType = EventType.AGENT_MESSAGE,
) -> Event:
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=event_type,
        data=data or {"content": {"text": "hi"}},
        actor=Actor(kind="system"),
        created_at=datetime.now(tz=UTC),
        trace_id=trace_id,
    )


def test_route_to_workflow_node_returns_row_or_none() -> None:
    """``_route_to_workflow_node`` returns the row for a matching trace, else None."""
    app = _FakeChatApp()
    adapter = EventAdapter(app)  # type: ignore[arg-type]
    row = WorkflowNodeRow(node_name="n", trace_id="tr_x")
    adapter._live_nodes_by_trace_and_invocation[("tr_x", "inv_A")] = row

    matched = _mk_event(trace_id="tr_x")
    result = adapter._route_to_workflow_node(matched)
    assert result is row

    unmatched = _mk_event(trace_id="tr_other")
    assert adapter._route_to_workflow_node(unmatched) is None


# ---------------------------------------------------------------------------
# Sub-test 14 (Reviewer #1 — routing safety)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwrap_mismatched_trace_id_drops_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A trace-id mismatch on the sub-agent dispatch drops the event."""
    app = _FakeChatApp()
    adapter = EventAdapter(app)  # type: ignore[arg-type]

    inner = _mk_event(
        trace_id="tr_B",
        data={"content": {"text": "should not fire"}},
    )

    with caplog.at_level(logging.WARNING, logger="cli.chat.event_adapter"):
        await adapter.dispatch_sub_agent_event(inner, outer_trace_id="tr_A")

    assert app.calls == []
    assert any(
        "trace mismatch" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Bonus: App satisfies the ChatMountSink Protocol structurally
# ---------------------------------------------------------------------------


def test_fake_app_conforms_to_chatmountsink() -> None:
    """The stub App satisfies ``ChatMountSink`` at runtime.

    The Protocol is ``runtime_checkable``; a plain isinstance check
    verifies every required method + shape lines up. bug-036d
    Reviewer #2: the type assertion migrates from a comment to an
    executed test.
    """
    app = _FakeChatApp()
    assert isinstance(app, ChatMountSink)


def test_workflow_node_row_conforms_to_chatmountsink() -> None:
    """The row satisfies ``ChatMountSink`` at runtime.

    Same runtime_checkable guard as the App test. Any drift in the
    row's mount method signatures shows up here.
    """
    row = WorkflowNodeRow(node_name="n")
    assert isinstance(row, ChatMountSink)
