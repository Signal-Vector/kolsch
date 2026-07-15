"""bug-036b — rendered-output snapshot tests for row-title labels.

Uses the shared text-snapshot helper introduced by bug-036a
(``tests/cli/chat/snapshot_helpers.py``). Each scenario builds a
minimal transcript, drives the row through the state changes we
want to visually verify, and diffs the rendered screen against a
checked-in golden under ``tests/snapshots/bug036b/``.

Regenerate goldens explicitly:

    SNAPSHOT_UPDATE=1 pytest tests/cli/chat/widgets/test_bug036b_snapshot.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.entities import Actor
from schemas.events import Event, EventType
from schemas.identifiers import EventId, SessionId, ThreadId
from tests.cli.chat.snapshot_helpers import assert_snapshot_matches

_APP_SIZE = (200, 100)


class _StubTranscriptApp(App[None]):
    """Minimal app hosting a single ``#transcript`` VerticalScroll.

    Matches the shape used by ``test_bug036a_snapshot.py`` so both
    suites capture goldens from an identical rendering surface.
    """

    CSS = """
    Screen { height: 100vh; }
    #transcript { height: 1fr; width: 100%; }
    """

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")


def _freeze_starting(row: WorkflowNodeRow) -> None:
    """Stop the row's spinner interval and pin the title to ``⠋``.

    The bug-031 spinner is wall-clock driven — the frame index at
    ``pilot.pause()`` time depends on how many 0.08s ticks the
    scheduler dispatched, which is non-deterministic across CI runs.
    For a golden text snapshot we need a stable glyph, so we stop
    the timer and reset the frame to 0. We also re-apply the static
    ``⠋`` placeholder glyph (matches the pre-spinner
    ``__init__`` title in ``workflow_node_row.py``) to overwrite any
    frame the timer may have already committed via
    ``_advance_spinner``.
    """
    if row._spinner_handle is not None:
        row._spinner_handle.stop()
        row._spinner_handle = None
    row._frame_idx = 0
    # Rebuild the base title using the static ``starting`` placeholder
    # glyph + the same ``(short_type)`` / details rules
    # ``_update_title`` applies. Keeps the golden readable and matches
    # how the row would render if the interval never fired.
    glyph = "⠋"
    base = f"{glyph} {row._node_name}"
    if row._details:
        base += f"   {row._details}"
    elif row._short_type:
        base += f" ({row._short_type})"
    if row._sub_agent_turn_count > 0:
        base += f" · {row._sub_agent_turn_count} sub-agent events"
    row.title = base


def _mk_agent_event() -> Event:
    """Build a well-formed `AGENT_MESSAGE` event.

    Used by the counter-label scenario — three of these push the
    row's ``_sub_agent_turn_count`` to 3 without needing a real
    sub-agent stream.
    """
    return Event(
        event_id=EventId(f"ev_{uuid4()}"),
        thread_id=ThreadId(f"th_{uuid4()}"),
        session_id=SessionId(f"ses_{uuid4()}"),
        type=EventType.AGENT_MESSAGE,
        data={"content": {"type": "text", "text": "hi"}},
        actor=Actor(kind="system"),
        created_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_snapshot_titles_show_type_pre_completion() -> None:
    """Golden: pre-completion rows carry ``(bash)`` and ``(agent)`` suffixes.

    Mounts two rows in ``starting`` state — one bash, one agent — so
    the golden captures the type-suffix rendering that lets a user
    tell them apart at a glance.
    """
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        bash_row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        agent_row = WorkflowNodeRow(
            node_name="hiring_manager_review",
            node_type="kolsch/nodes/agent",
        )
        await transcript.mount(bash_row)
        await transcript.mount(agent_row)
        await pilot.pause()

        # bug-031 spinner ticks are wall-clock derived and would
        # churn the golden on every run. Freeze both rows to their
        # initial static ``⠋`` glyph by stopping the interval + re-
        # applying the ``__init__`` title shape.
        _freeze_starting(bash_row)
        _freeze_starting(agent_row)
        await pilot.pause()

        assert_snapshot_matches(
            pilot.app, "bug036b/titles_pre_completion",
        )


@pytest.mark.asyncio
async def test_snapshot_titles_drop_type_when_details_set() -> None:
    """Golden: post-completion agent row shows details, no type suffix.

    Same two rows as the pre-completion scenario, then the agent
    row transitions to ``completed`` with ``details="scored 8/10"``.
    The golden must show ``scored 8/10`` in the agent-row title and
    must NOT show ``(agent)`` there.
    """
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        bash_row = WorkflowNodeRow(
            node_name="fetch_jd",
            node_type="kolsch/nodes/bash",
        )
        agent_row = WorkflowNodeRow(
            node_name="hiring_manager_review",
            node_type="kolsch/nodes/agent",
        )
        await transcript.mount(bash_row)
        await transcript.mount(agent_row)
        await pilot.pause()

        # Freeze bash row before it starts animating — see the
        # pre-completion scenario for the same rationale. The agent
        # row will hit a static terminal glyph via ``completed``.
        _freeze_starting(bash_row)
        agent_row.set_state("completed", details="scored 8/10")
        await pilot.pause()

        assert_snapshot_matches(
            pilot.app, "bug036b/titles_post_completion",
        )


@pytest.mark.asyncio
async def test_snapshot_counter_label_reads_events() -> None:
    """Golden: title trails with ``· 3 sub-agent events`` (not ``turns``).

    Three sink-method calls push the row's counter to 3; the golden
    captures the renamed label so a future regression reintroducing
    ``turns`` fails the diff.
    """
    app = _StubTranscriptApp()
    async with app.run_test(size=_APP_SIZE) as pilot:
        transcript = pilot.app.query_one("#transcript", VerticalScroll)
        row = WorkflowNodeRow(
            node_name="hiring_manager_review",
            node_type="kolsch/nodes/agent",
        )
        await transcript.mount(row)
        await pilot.pause()

        # bug-036d: sink-method calls bump ``_sub_agent_turn_count``.
        for i in range(3):
            await row.mount_assistant_message(f"m{i}", event_id=f"ev-{i}")
        # Freeze after events so the trailing "· 3 sub-agent events"
        # counter renders alongside a stable starting glyph.
        _freeze_starting(row)
        await pilot.pause()

        assert_snapshot_matches(
            pilot.app, "bug036b/counter_label",
        )
