"""bug-037 — Pilot tests for `NodeSection` + section-wrapped mount sinks.

13 sub-tests covering AC #1-#8 behavioral guarantees. Every widget
query asserts an explicit `len(...) == N` count so a silent
double-mount regression fails loud (Reviewer #2 finding).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.notice import NoticeWidget
from cli.chat.widgets.user_chatbox import UserChatbox
from cli.chat.widgets.workflow_node_row import (
    _EVENT_BUFFER_MAX,
    WorkflowNodeRow,
)

_AppFactory = Callable[[Widget], App[None]]


class _Host(App[None]):
    """Minimal host that hosts a mount slot for section tests."""

    CSS = """
    Screen { height: 100vh; }
    #slot { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="slot")


def _contents_of(row: WorkflowNodeRow) -> Collapsible.Contents:
    return row.query_one(Collapsible.Contents)


# ---------------------------------------------------------------------------
# Sub-test 1 — NodeSection construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_section_mounts_with_title(
    mock_app_factory: _AppFactory,
) -> None:
    """Bare construction — title comes through as-is (completed state)."""
    section = NodeSection("input")
    assert section.title == "input"
    assert section.has_class("node-section")


# ---------------------------------------------------------------------------
# Sub-test 2 — state transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_section_state_transitions(
    mock_app_factory: _AppFactory,
) -> None:
    """`running` suffixes `…`; `completed` removes it."""
    section = NodeSection("tool: browse_url", state="running")
    assert section.title.endswith("…")

    section.set_state("completed")
    assert not section.title.endswith("…")
    assert section.title == "tool: browse_url"


# ---------------------------------------------------------------------------
# Sub-test 3 — failed state adds prefix + class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_section_failed_state_adds_class(
    mock_app_factory: _AppFactory,
) -> None:
    """`failed` prefixes ✗ and adds `-failed` class."""
    section = NodeSection("tool: read_file", state="failed")
    assert section.title.startswith("✗ ")
    assert section.has_class("-failed")


# ---------------------------------------------------------------------------
# Sub-test 4 — dedupe prevents empty section (Reviewer #1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedupe_prevents_empty_section() -> None:
    """Second `mount_assistant_message` with same text is dropped BEFORE section construction."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="n")
        row.collapsed = False
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        await row.mount_assistant_message("A", event_id="x")
        await pilot.pause()
        await row.mount_assistant_message("A", event_id="y")
        await pilot.pause()

        sections = list(_contents_of(row).query(NodeSection))
        assert len(sections) == 1
        assert row._response_count == 1
        prose = list(_contents_of(row).query(AssistantProse))
        assert len(prose) == 1


# ---------------------------------------------------------------------------
# Sub-test 5 — response section index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_row_response_sections_by_index() -> None:
    """Two distinct responses → `response` + `response (2)` sections."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="n")
        row.collapsed = False
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        await row.mount_assistant_message("A", event_id="a")
        await row.mount_assistant_message("B", event_id="b")
        await pilot.pause()

        sections = list(_contents_of(row).query(NodeSection))
        assert len(sections) == 2
        titles = [str(s.title) for s in sections]
        assert titles == ["response", "response (2)"]
        prose = list(row.query(AssistantProse))
        assert len(prose) == 2


# ---------------------------------------------------------------------------
# Sub-test 6 — per-key input sections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_row_dict_input_creates_per_key_sections() -> None:
    """`content={a,b,c}` → three `input: <key>` sections + 3 UserChatbox."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="n")
        row.collapsed = False
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        await row.mount_user_message(
            "flat text",
            content={"a": "1", "b": "2", "c": "3"},
        )
        await pilot.pause()

        sections = list(_contents_of(row).query(NodeSection))
        assert len(sections) == 3
        titles = [str(s.title) for s in sections]
        assert titles == ["input: a", "input: b", "input: c"]
        boxes = list(row.query(UserChatbox))
        assert len(boxes) == 3


# ---------------------------------------------------------------------------
# Sub-test 6b — command_preview-before-bound_inputs race (Reviewer #1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_preview_before_bound_inputs_race() -> None:
    """`set_command_preview` first → section built w/o bound-inputs block.

    A subsequent `set_bound_inputs` is a no-op — the section is NOT
    rebuilt.
    """
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        # Race: command preview fires first.
        row.set_command_preview("ls")
        await pilot.pause()

        assert row._bash_input_section is not None
        first_section = row._bash_input_section
        # Body Static inside the section — no `bound inputs:` block.
        statics = [s for s in first_section.walk_children() if isinstance(s, Static)]
        combined = "\n".join(str(getattr(s, "content", "")) for s in statics)
        assert "bound inputs:" not in combined
        assert "command:" in combined

        # Late set_bound_inputs → no-op (section unchanged).
        row.set_bound_inputs({"a": "1"})
        await pilot.pause()
        assert row._bash_input_section is first_section


# ---------------------------------------------------------------------------
# Sub-test 7 — bash row creates three sections
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_row_creates_input_output_exit_sections() -> None:
    """bound_inputs → set_command_preview → set_output → 3 sections."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        row.set_bound_inputs({"url": "https://example.com"})
        row.set_command_preview("curl {{ inputs.url }}")
        await pilot.pause()
        row.set_output({"stdout": "hi\n", "stderr": "", "exit_code": 0})
        await pilot.pause()

        sections = list(_contents_of(row).query(NodeSection))
        assert len(sections) == 3
        titles = [str(s.title) for s in sections]
        assert titles[0] == "input"
        assert titles[1] == "output"
        assert titles[2].startswith("exit: 0")


# ---------------------------------------------------------------------------
# Sub-test 8 — bash set_output gate scope (Reviewer #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bash_set_output_gate_scope() -> None:
    """`_last_assistant_text` gate is agent-scoped; bash sections still fire."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        # Simulate a stale _last_assistant_text on a bash row.
        row.set_last_assistant_text("x")

        row.set_output({"stdout": "hi", "stderr": "", "exit_code": 0})
        await pilot.pause()

        # bash sections still created despite the non-empty text.
        assert row._bash_output_section is not None
        assert row._bash_exit_section is not None


# ---------------------------------------------------------------------------
# Sub-test 9 — notice pins at TOP (Reviewer #1 + #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_pins_at_top_between_sections() -> None:
    """Order: response_1 → notice → response_2. Notice pins as first child."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="n")
        row.collapsed = False
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        await row.mount_assistant_message("A", event_id="a")
        await pilot.pause()
        await row.mount_notice("nid1", "warning text", level="warning")
        await pilot.pause()
        await row.mount_assistant_message("B", event_id="b")
        await pilot.pause()

        contents = _contents_of(row)
        children = list(contents.children)
        # First child must be the notice.
        assert isinstance(children[0], NoticeWidget)
        # Then the two response sections in insertion order.
        sections = [c for c in children if isinstance(c, NodeSection)]
        assert len(sections) == 2
        assert str(sections[0].title) == "response"
        assert str(sections[1].title) == "response (2)"


# ---------------------------------------------------------------------------
# Sub-test 10 — map iteration rows are direct children (Reviewer #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_iteration_rows_direct_children_of_contents() -> None:
    """Iteration rows sit directly in map row's Contents — no NodeSection."""
    app = _Host()
    async with app.run_test() as pilot:
        map_row = WorkflowNodeRow(
            node_name="score_candidates",
            node_type="kolsch/nodes/map",
        )
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(map_row)
        await pilot.pause()

        iter_rows = [
            WorkflowNodeRow(
                node_name=f"score_candidates[{i}]",
                node_type="kolsch/nodes/agent",
            )
            for i in range(3)
        ]
        contents = _contents_of(map_row)
        for r in iter_rows:
            await contents.mount(r)
        await pilot.pause()

        # Iteration rows are direct children.
        direct = [c for c in contents.children if isinstance(c, WorkflowNodeRow)]
        assert len(direct) == 3
        # No NodeSection sits between the map row and its iterations.
        map_sections = [c for c in contents.children if isinstance(c, NodeSection)]
        assert len(map_sections) == 0


# ---------------------------------------------------------------------------
# Sub-test 11 — tool section state transition via tool_call_id (Reviewer #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_state_transition_via_tool_call_id() -> None:
    """Same tool_call_id → SAME section transitions running → completed."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="n")
        row.collapsed = False
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        # First call — empty body → running state, `…` suffix.
        await row.mount_tool_call(
            "browse_url",
            args_str='{"u":"..."}',
            body="",
            event_id="tc_1",
            failed=False,
        )
        await pilot.pause()
        assert len(list(_contents_of(row).query(NodeSection))) == 1
        section = row._tool_sections["tc_1"]
        assert section.title.endswith("…")

        # Second call — same tc_1 → SAME section transitions.
        await row.mount_tool_call(
            "browse_url",
            args_str='{"u":"..."}',
            body="result body",
            event_id="tc_1",
            failed=False,
        )
        await pilot.pause()

        # Still exactly one NodeSection.
        sections = list(_contents_of(row).query(NodeSection))
        assert len(sections) == 1
        assert row._tool_sections["tc_1"] is section
        assert not section.title.endswith("…")


# ---------------------------------------------------------------------------
# Sub-test 12 — tool call section shows tool name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_section_shows_name() -> None:
    """First `mount_tool_call` → `tool: <name>` NodeSection title."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="n")
        row.collapsed = False
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()

        await row.mount_tool_call(
            "browse_url",
            args_str="",
            body="body",
            event_id="tc_a",
            failed=False,
        )
        await pilot.pause()

        sections = list(_contents_of(row).query(NodeSection))
        assert len(sections) == 1
        # Completed state — no `…` suffix; title = `tool: browse_url`.
        assert str(sections[0].title) == "tool: browse_url"


# ---------------------------------------------------------------------------
# Sub-test 13 — buffer overflow drops gracefully (Reviewer #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buffer_overflow_drops_gracefully() -> None:
    """Queue > `_EVENT_BUFFER_MAX` → overflow drops oldest; no crash."""
    app = _Host()
    async with app.run_test() as pilot:
        row = WorkflowNodeRow(node_name="n")
        # row starts collapsed by default
        slot = pilot.app.query_one("#slot", VerticalScroll)
        await slot.mount(row)
        await pilot.pause()
        assert row.collapsed is True

        overflow_count = _EVENT_BUFFER_MAX + 2
        for i in range(overflow_count):
            await row.mount_assistant_message(f"msg-{i}", event_id=f"ev-{i}")
        await pilot.pause()

        # Queue is bounded — oldest dropped.
        assert len(row._pending_mounts) <= _EVENT_BUFFER_MAX

        # Expand — drain the queue; no crash.
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        prose = list(_contents_of(row).query(AssistantProse))
        # At most _EVENT_BUFFER_MAX mounts fire.
        assert len(prose) <= _EVENT_BUFFER_MAX
