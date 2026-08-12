"""bug-036a — rendered-output snapshot tests (Layer 2 infrastructure).

Snapshots capture the app's actual rendered screen text (via
``app.screen.render_lines``) and diff against a checked-in
``.txt`` golden. This catches mount-tree bugs, visibility bugs,
and content bugs — the classes bug-035 shipped past because
``row.query(AssistantProse)`` returns a widget regardless of
whether it landed inside ``Contents`` (bug-035's root cause) or
is visible at all.

Update goldens explicitly:

    SNAPSHOT_UPDATE=1 pytest tests/cli/chat/test_bug036a_snapshot.py

Any future regression is a character-level diff against the
golden files under ``tests/snapshots/bug036a/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.event_adapter import EventAdapter, _is_card_mounting_tool
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from tests.cli.chat.snapshot_helpers import assert_snapshot_matches

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "sessions"
    / "ses_bug036a_jd_review_excerpt.jsonl"
)
_APP_SIZE = (200, 100)


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
    """Same stub used by ``test_bug036a_fixture_replay.py``.

    A ``#transcript`` VerticalScroll is the only widget the adapter
    needs to mount CardWidgets into; every other ``ChatApp`` surface
    is a no-op stub. This keeps the snapshot golden focused on
    exactly what bug-036a changes — the transcript region.

    CSS: make the transcript fill the screen height + width so the
    snapshot captures the mounted widgets. Without this the
    VerticalScroll auto-sizes to ``height: auto`` and the golden
    ends up blank.
    """

    CSS = """
    Screen { height: 100vh; }
    #transcript { height: 1fr; width: 100%; }
    """

    def __init__(self) -> None:
        super().__init__()
        # spec-130 Step 5 review fixup: this stub's `mount_tool_call`
        # was already a pure no-op (no DOM mount, no tracking) — the
        # snapshot golden alone was never sensitive to a card-mounting
        # tool wrongly reaching a mount call, since neither branch
        # touches the DOM here. Track calls the same way
        # `test_bug036a_fixture_replay.py`'s `_StubTranscriptApp` does
        # so `test_snapshot_dedupe_transcript_all_expanded`/
        # `_all_collapsed` (which replay the SAME
        # `ses_bug036a_jd_review_excerpt.jsonl` fixture, containing the
        # same card-mounting `run_workflow*` call) can assert AC #1
        # directly instead of relying only on the golden.
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
        no-op stub would make a wrongly-taken branch invisible to
        both AC #1 enforcement mechanisms — this tracked list and any
        golden-snapshot diff, since neither branch mounts a real
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


async def _replay_fixture(app: _StubTranscriptApp) -> EventAdapter:
    adapter = EventAdapter(app)
    for row in _load_fixture():
        event = _event_from_row(row)
        await adapter.handle(event)
    return adapter


@pytest.mark.asyncio
async def test_snapshot_dedupe_transcript_all_expanded() -> None:
    """Golden: fixture replay with every node row expanded.

    Expected content: workflow card body visible, each agent node's
    ``AssistantProse`` rendered exactly once (no JSON preview
    underneath), no standalone ``ToolCallBlock`` for the
    ``run_workflow*`` invocation.
    """
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        await _replay_fixture(app)
        await pilot.pause()

        # AC #1 (same invariant `test_bug036a_fixture_replay.py`
        # asserts on the same fixture): no transcript-root
        # ToolCallBlock mount for a card-mounting tool.
        for tool_name, _args, _body in app.mounted_tool_calls:
            assert not _is_card_mounting_tool(tool_name), (
                f"transcript-root ToolCallBlock mounted for "
                f"card-mounting tool {tool_name!r}; AC #1 violated"
            )

        for row in pilot.app.query(WorkflowNodeRow):
            row.collapsed = False
        await pilot.pause()

        assert_snapshot_matches(
            pilot.app,
            "bug036a/dedupe_transcript_all_expanded",
        )


@pytest.mark.asyncio
async def test_snapshot_dedupe_transcript_all_collapsed() -> None:
    """Golden: fixture replay with every row still collapsed.

    Expected content: workflow card + node row titles only, no
    bodies visible. Catches "widget landed as sibling of Contents"
    — a sibling would remain visible when collapsed, and the
    golden would contain response text where it shouldn't.
    """
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        await _replay_fixture(app)
        await pilot.pause()
        # Rows are collapsed by default — no expand needed.

        # AC #1 (same invariant `test_bug036a_fixture_replay.py`
        # asserts on the same fixture): no transcript-root
        # ToolCallBlock mount for a card-mounting tool.
        for tool_name, _args, _body in app.mounted_tool_calls:
            assert not _is_card_mounting_tool(tool_name), (
                f"transcript-root ToolCallBlock mounted for "
                f"card-mounting tool {tool_name!r}; AC #1 violated"
            )

        assert_snapshot_matches(
            pilot.app,
            "bug036a/dedupe_transcript_all_collapsed",
        )


@pytest.mark.asyncio
async def test_snapshot_late_message_race_fallback() -> None:
    """Golden: bug-032-shape late-message race falls back to set_output.

    Simulates AC #4 sub-test 2: ``_on_node_completed`` fires first
    (freezing the row + running the JSON-preview ``set_output``),
    then a late assistant-message arrives via the sink method —
    dropped by ``_buffer_frozen``. The gate does NOT fire because
    ``_last_assistant_text`` stayed empty; the ``set_output`` preview
    remains mounted, so the row is never blank. Golden shows the
    JSON-preview text under the row.
    """
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        await pilot.pause()

        # Build the row in isolation — this scenario is per-row, not
        # a full fixture replay.
        row = WorkflowNodeRow(
            node_name="review",
            node_type="kolsch/nodes/agent",
        )
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        await transcript.mount(row)
        await pilot.pause()

        # Race: NODE_COMPLETED first. ``set_state("success")``
        # freezes further mounts; ``set_output`` mounts the JSON
        # preview because ``_last_assistant_text`` is empty.
        row.set_state("success")
        row.set_output({"summary": "the answer is 42"})
        await pilot.pause()

        # Late assistant message via the sink — dropped by
        # ``_buffer_frozen``.
        await row.mount_assistant_message(
            "the answer is 42",
            event_id="ev_late",
        )

        # Expand so the JSON preview is visible for the snapshot.
        row.collapsed = False
        await pilot.pause()

        assert_snapshot_matches(
            pilot.app,
            "bug036a/late_race_shows_output_widget",
        )
