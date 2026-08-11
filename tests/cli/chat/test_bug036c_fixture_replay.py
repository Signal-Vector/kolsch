"""bug-036c — flagship fixture-replay test.

Replays the shared bug-036a JSONL fixture (extended to include a
bash-node slice) through the real
:class:`~cli.chat.event_adapter.EventAdapter` and asserts:

- **AC #1:** the ``WorkflowCard`` header contains the workflow ref on
  its first (or first-flattened) line and an ``inputs:`` summary on
  its second line.
- **AC #4:** the bash row's ``Collapsible.Contents`` contains a
  ``Static`` with class ``node-command-preview`` whose text starts
  with ``↳ command:``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static

from cli.chat.event_adapter import EventAdapter, _is_card_mounting_tool
from cli.chat.widgets.card_body.workflow import WorkflowProgressHeader
from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "sessions"
    / "ses_bug036a_jd_review_excerpt.jsonl"
)


def _load_fixture() -> list[dict[str, Any]]:
    return [json.loads(line) for line in _FIXTURE_PATH.read_text().splitlines() if line]


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
    """Same shape as ``test_bug036a_fixture_replay._StubTranscriptApp``.

    A ``#transcript`` VerticalScroll is the only mount slot; every
    other ChatApp surface is a no-op stub. CSS makes the transcript
    fill the screen so the header + row content is actually painted.
    """

    CSS = """
    Screen { height: 100vh; }
    #transcript { height: 1fr; width: 100%; }
    """

    def __init__(self) -> None:
        super().__init__()
        # spec-130 Step 5 review fixup: this stub's `mount_tool_call`
        # was already a pure no-op (no DOM mount, no tracking). Track
        # calls the same way `test_bug036a_fixture_replay.py`'s stub
        # does so this test can assert bug-036a's AC #1 (no
        # transcript-root ToolCallBlock for a card-mounting tool) on
        # the SAME shared fixture, not just its own AC #1/#4.
        self.mounted_tool_calls: list[tuple[str, ...]] = []

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")

    async def mount_user_message(
        self,
        text: str,
        timestamp: Any = None,
        *,
        event_id: Any = None,
        content: Any = None,
    ) -> None:
        _ = text, timestamp, event_id, content

    async def mount_assistant_message(
        self,
        text: str,
        *,
        event_id: Any = None,
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
        _ = failed, recovery_hint, event_id
        self.mounted_tool_calls.append((tool_name, args, body))

    async def mount_tool_call_running(self, tool_name: str, args: str) -> None:
        """Record a running-block mount the SAME way `mount_tool_call` does.

        spec-130 Step 5 review fixup: `_handle_tool_called` calls this
        directly for non-card-mounting tools only — the fixture's
        `run_workflow*` call is card-mounting, so the correctly-
        behaving adapter takes `clear_working_placeholder` below, not
        this one. Must append to `mounted_tool_calls` (not no-op): a
        no-op stub would make a wrongly-taken branch invisible to the
        AC #1 assertion below, since neither branch mounts a real
        widget in this stub either way.
        """
        self.mounted_tool_calls.append((tool_name, args, ""))

    async def clear_working_placeholder(self) -> None:
        # spec-130 Step 5 (fixup): `_handle_tool_called` calls this
        # for card-mounting tool calls — confirmed by instrumentation
        # this IS the branch exercised by the shared fixture's
        # `run_workflow*` call — instead of `mount_tool_call_running`,
        # so nothing orphans `_working_placeholder`. This stub has no
        # placeholder concept; a no-op satisfies the call.
        return None

    async def mount_notice(
        self,
        notice_id: str,
        text: str,
        *,
        level: str = "info",
        event_id: Any = None,
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


@pytest.mark.asyncio
async def test_fixture_replay_header_and_bash_command() -> None:
    """Replay the fixture; assert AC #1 header + AC #4 bash command."""
    fixture = _load_fixture()
    app = _StubTranscriptApp()
    async with app.run_test() as pilot:
        adapter = EventAdapter(app)
        for row in fixture:
            event = _event_from_row(row)
            await adapter.handle(event)
        await pilot.pause()

        # bug-036a AC #1 (same invariant, same shared fixture): no
        # transcript-root ToolCallBlock mount for a card-mounting
        # tool. Distinct from this file's own "AC #1" (header content,
        # below).
        for tool_name, _args, _body in app.mounted_tool_calls:
            assert not _is_card_mounting_tool(tool_name), (
                f"transcript-root ToolCallBlock mounted for "
                f"card-mounting tool {tool_name!r}; bug-036a AC #1 violated"
            )

        # AC #1: header carries the workflow ref + inputs line.
        headers = list(pilot.app.query(WorkflowProgressHeader))
        assert headers, "no WorkflowProgressHeader mounted"
        header = headers[0]
        text = header._compute_text()
        lines = text.split("\n")
        assert len(lines) == 3, (
            f"expected 3-line header (ref, inputs, progress); got {lines!r}"
        )
        first = lines[0]
        # Top-level fixture has no parent; first line is the raw ref.
        assert first and not first.startswith("["), (
            f"header first line should carry the ref, got {first!r}"
        )
        assert lines[1].startswith("inputs:"), (
            f"header second line should be inputs: summary; got {lines[1]!r}"
        )

        # AC #4: expand rows so bash row's command preview is visible.
        rows = list(pilot.app.query(WorkflowNodeRow))
        assert rows, "no WorkflowNodeRow mounted"
        for r in rows:
            r.collapsed = False
        await pilot.pause()

        bash_rows = [r for r in rows if r._node_type == "kolsch/nodes/bash"]
        assert bash_rows, (
            "fixture must include a bash-type node — otherwise AC #4 "
            "surface is trivially vacuous"
        )
        bash_row = bash_rows[0]
        contents = bash_row.query_one(Collapsible.Contents)
        previews = [
            c
            for c in contents.walk_children()
            if isinstance(c, Static) and c.has_class("node-command-preview")
        ]
        # qol-108: post-split a bash row mounts a ``▶ command`` section
        # (always, when a command preview is delivered) and optionally a
        # ``▶ input`` section (when binds / workflow-inputs-used are
        # present). The command widget lives inside ``▶ command``; the
        # section title is now the ``command`` label, so the body no
        # longer carries a redundant ``command:`` prefix.
        assert previews, "bash row should carry at least one node-command-preview"
        preview_text = "\n".join(str(p.render()) for p in previews)
        assert preview_text.strip(), (
            f"preview should carry non-empty body text; got {preview_text!r}"
        )

        # Sanity: fixture actually opened a workflow card.
        workflow_cards = [
            w
            for w in pilot.app.query(CardWidget)
            if getattr(w, "kind", None) == "workflow"
        ]
        assert workflow_cards, "no WorkflowCard mounted"
