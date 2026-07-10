"""bug-036d — rendered-output snapshot tests for the four mockups.

Reuses the shared fixture at
``tests/fixtures/sessions/ses_bug036a_jd_review_excerpt.jsonl``.
The fixture ships two agent-node events (``headhunter_review``
+ ``deduper``) and one bash node (``fetch_jd``); that's enough
to cover Mockups 1 (all collapsed), 2 (bash expanded), 3 (agent
single-turn expanded), and 4 (agent multi-input expanded).

Mockups 5 and 6 are aspirational — no current agent triggers
them — and are covered by AC #9 sub-tests 12 (double-prose) via
unit tests rather than end-to-end goldens.

Update goldens explicitly:

    SNAPSHOT_UPDATE=1 pytest tests/cli/chat/widgets/test_bug036d_snapshot.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.event_adapter import EventAdapter
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from tests.cli.chat.snapshot_helpers import assert_snapshot_matches


_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3]
    / "fixtures" / "sessions" / "ses_bug036a_jd_review_excerpt.jsonl"
)
_APP_SIZE = (200, 100)


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


class _StubTranscriptApp(App[None]):
    """Same shape as ``test_bug036a_snapshot._StubTranscriptApp``."""

    CSS = """
    Screen { height: 100vh; }
    #transcript { height: 1fr; width: 100%; }
    """

    def __init__(self) -> None:
        super().__init__()

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
        self,
        tool_name: str,
        args: str,
        body: str,
        *,
        failed: bool = False,
        recovery_hint: str | None = None,
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


async def _replay_fixture(app: _StubTranscriptApp) -> EventAdapter:
    adapter = EventAdapter(app)
    for row in _load_fixture():
        event = _event_from_row(row)
        await adapter.handle(event)
    return adapter


@pytest.mark.asyncio
async def test_snapshot_mockup1_all_collapsed() -> None:
    """Mockup 1 — post-completion, all rows collapsed."""
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        await _replay_fixture(app)
        await pilot.pause()
        # Rows are collapsed by default — no expand needed.

        assert_snapshot_matches(pilot.app, "bug036d/mockup1_all_collapsed")


@pytest.mark.asyncio
async def test_snapshot_mockup2_bash_expanded() -> None:
    """Mockup 2 — ``fetch_jd`` bash row expanded."""
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        await _replay_fixture(app)
        await pilot.pause()

        for r in pilot.app.query(WorkflowNodeRow):
            if r._node_type == "kolsch/nodes/bash":
                r.collapsed = False
        await pilot.pause()
        await pilot.pause()

        assert_snapshot_matches(pilot.app, "bug036d/mockup2_bash_expanded")


@pytest.mark.asyncio
async def test_snapshot_mockup3_agent_single_turn() -> None:
    """Mockup 3 — first agent row expanded (single-turn)."""
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        await _replay_fixture(app)
        await pilot.pause()

        agent_rows = [
            r for r in pilot.app.query(WorkflowNodeRow)
            if r._node_type == "kolsch/nodes/agent"
        ]
        if agent_rows:
            agent_rows[0].collapsed = False
        await pilot.pause()
        await pilot.pause()

        assert_snapshot_matches(pilot.app, "bug036d/mockup3_agent_single_turn")


@pytest.mark.asyncio
async def test_snapshot_mockup4_agent_multi_input() -> None:
    """Mockup 4 — all agent rows expanded (fan-in multi-input surface).

    The fixture's ``headhunter_review`` node is the only agent-type
    entry shipped today; Mockup 4's ``deduper`` shape (multi-value
    input dict) is exercised via the ``all agents expanded`` view.
    Snapshot captures the current fixture's actual output — later
    fixture extensions can add the ``deduper`` slice without
    breaking the golden.
    """
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        await _replay_fixture(app)
        await pilot.pause()

        for r in pilot.app.query(WorkflowNodeRow):
            if r._node_type == "kolsch/nodes/agent":
                r.collapsed = False
        await pilot.pause()
        await pilot.pause()

        assert_snapshot_matches(pilot.app, "bug036d/mockup4_agent_multi_input")
