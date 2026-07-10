"""bug-037 — fixture-replay assertions for section layout.

Replays the shared JD-review fixture and asserts the section shape
per node type per AC #12:

- Bash row (`fetch_jd`) → `input`, `output`, `exit: ...` sections.
- Agent rows (`headhunter_review`, `deduper`) → per-key `input: <key>`
  section(s) + `response` section.
- Deduper specifically → 3 `input: <key>` sections.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.event_adapter import EventAdapter
from cli.chat.widgets.node_section import NodeSection
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures" / "sessions" / "ses_bug036a_jd_review_excerpt.jsonl"
)


def _load_fixture() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in _FIXTURE_PATH.read_text().splitlines()
        if line
    ]


def _event_from_row(row: dict[str, Any]) -> Event:
    return Event(
        event_id=EventId(row["event_id"]),
        parent_event_id=row.get("parent_event_id"),
        thread_id=ThreadId(row["thread_id"]),
        session_id=SessionId(row["session_id"]),
        type=EventType(row["type"]),
        data=row.get("data") or {},
        actor=Actor(**(row.get("actor") or {"kind": "system"})),
        created_at=row["created_at"],
        trace_id=row.get("trace_id"),
    )


class _StubApp(App[None]):
    CSS = """
    Screen { height: 100vh; }
    #transcript { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")

    async def mount_user_message(
        self, text: str, timestamp: Any = None,
        *, event_id: Any = None, content: Any = None,
    ) -> None:
        _ = text, timestamp, event_id, content

    async def mount_assistant_message(
        self, text: str, *, event_id: Any = None,
    ) -> None:
        _ = text, event_id

    async def mount_assistant_streaming_widget(self) -> Any:
        class _P:
            async def append(self, text: str) -> None:
                _ = text
        return _P()

    async def mount_tool_call(
        self, tool_name: str, args: str, body: str,
        *, failed: bool = False, recovery_hint: str | None = None,
        event_id: Any = None,
    ) -> None:
        _ = tool_name, args, body, failed, recovery_hint, event_id

    async def mount_notice(
        self, notice_id: str, text: str,
        *, level: str = "info", event_id: Any = None,
    ) -> None:
        _ = notice_id, text, level, event_id

    async def mount_exchange_failed(self, message: str) -> None:
        _ = message

    async def mount_approval_block(self, *args: Any, **kwargs: Any) -> Any:
        _ = args, kwargs
        return None

    async def set_turn_verb(self, verb: str, *, target: str = "") -> None:
        _ = verb, target

    def clear_awaiting_approval(self) -> None:
        return None

    def last_assistant_text(self) -> str:
        return ""

    def set_last_assistant_text(self, text: str) -> None:
        _ = text


async def _replay(app: _StubApp) -> EventAdapter:
    adapter = EventAdapter(app)
    for row in _load_fixture():
        await adapter.handle(_event_from_row(row))
    return adapter


def _sections_of(row: WorkflowNodeRow) -> list[NodeSection]:
    return list(row.query(NodeSection))


@pytest.mark.asyncio
async def test_fixture_replay_bash_row_has_three_sections() -> None:
    """Bash row gets `input`, `output`, `exit: 0` sections."""
    app = _StubApp()
    async with app.run_test(size=(200, 100)) as pilot:
        await _replay(app)
        await pilot.pause()

        bash_rows = [
            r for r in pilot.app.query(WorkflowNodeRow)
            if r._node_type == "kolsch/nodes/bash"
        ]
        assert len(bash_rows) == 1
        row = bash_rows[0]
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()
        titles = [str(s.title) for s in _sections_of(row)]
        assert "input" in titles
        assert "output" in titles
        assert any(t.startswith("exit:") for t in titles)


@pytest.mark.asyncio
async def test_fixture_replay_agent_rows_have_input_and_response() -> None:
    """Each agent row has at least one `input: <key>` + one `response`."""
    app = _StubApp()
    async with app.run_test(size=(200, 100)) as pilot:
        await _replay(app)
        await pilot.pause()

        agent_rows = [
            r for r in pilot.app.query(WorkflowNodeRow)
            if r._node_type == "kolsch/nodes/agent"
        ]
        assert len(agent_rows) >= 1
        for row in agent_rows:
            row.collapsed = False
        await pilot.pause()
        await pilot.pause()
        for row in agent_rows:
            titles = [str(s.title) for s in _sections_of(row)]
            input_titles = [t for t in titles if t.startswith("input")]
            response_titles = [t for t in titles if t.startswith("response")]
            assert len(input_titles) >= 1, (
                f"{row._node_name}: no input sections; got {titles}"
            )
            assert len(response_titles) >= 1, (
                f"{row._node_name}: no response sections; got {titles}"
            )


@pytest.mark.asyncio
async def test_fixture_replay_deduper_has_three_input_sections() -> None:
    """Deduper row has 3 `input: <key>` sections (multi-input fan-in)."""
    app = _StubApp()
    async with app.run_test(size=(200, 100)) as pilot:
        await _replay(app)
        await pilot.pause()

        deduper_rows = [
            r for r in pilot.app.query(WorkflowNodeRow)
            if r._node_name == "deduper"
        ]
        assert len(deduper_rows) == 1
        row = deduper_rows[0]
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()
        input_titles = [
            str(s.title) for s in _sections_of(row)
            if str(s.title).startswith("input")
        ]
        assert len(input_titles) == 3
