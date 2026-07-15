"""feat-019 Test 2 — Status line 2 rendering (Pilot).

Four passes covering the StatusBar's reactive contract + the layout
manager's push into it:

1. No live workflow → StatusBar's ``-visible`` class is absent (the
   row is hidden by CSS).
2. Layout manager flush with a running workflow → StatusBar's inner
   text carries the strip and the ``-visible`` class is present.
3. ``turn_active`` set while a workflow is live → strip is suppressed
   (turn takes precedence — Coordinator Q1 lock).
4. Workflow terminal → StatusBar drops back to hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from cli.chat.services.workflow_layout import WorkflowLayoutManager
from cli.chat.services.workflows_impl import WorkflowRunRecord
from cli.chat.widgets.status_bar import StatusBar
from schemas.identifiers import CardId
from tools.services.workflows import NodeCounts
from workflows.ids import WorkflowRunId

_NOW = datetime.now(tz=UTC)


@dataclass
class _MockCard:
    id: CardId
    is_closed: bool = False


class _HostApp(App):
    """Minimal host — transcript on top, StatusBar in the bottom stack."""

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        with Vertical(id="bottom-stack"):
            yield StatusBar(id="status-bar")


def _mk_record(
    *,
    suffix: str,
    phase: str,
    card: _MockCard | None,
    ref: str = "test/ref",
    completed: int = 0,
    total: int = 0,
    started_offset: int = 0,
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=WorkflowRunId(f"wrk_00000000-0000-0000-0000-0000000000{suffix}"),
        ref=ref,
        phase=phase,  # type: ignore[arg-type]
        card=card,  # type: ignore[arg-type]
        created_at=_NOW,
        started_at=_NOW - timedelta(seconds=started_offset),
        node_counts=NodeCounts(total=total, completed=completed),
    )


@pytest.mark.asyncio
async def test_no_live_workflow_status_bar_hidden() -> None:
    """Empty registry → StatusBar has no ``-visible`` class."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        status_bar = app.query_one("#status-bar", StatusBar)
        assert not status_bar.has_class("-visible")


@pytest.mark.asyncio
async def test_layout_manager_push_shows_strip() -> None:
    """After a flush with a running workflow, StatusBar renders the strip."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)
        status_bar = app.query_one("#status-bar", StatusBar)

        card = _MockCard(id=CardId("crd_00000000-0000-0000-0000-000000000001"))
        record = _mk_record(
            suffix="a1",
            phase="running",
            card=card,
            ref="e2e/ref",
            completed=3,
            total=5,
            started_offset=10,
        )
        widget_map: dict[Any, Any] = {}

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: [record],
            get_card_widget=lambda cid: widget_map.get(cid),
            reorder_tick=0.02,
            status_bar=status_bar,
        )

        # Direct flush to bypass the timer (deterministic).
        await mgr._flush_reorder()
        await pilot.pause()

        assert status_bar.has_class("-visible")
        # Inner text carries the strip.
        text = str(status_bar.query_one("#status-bar-text", Static).render())
        assert "e2e/ref 3/5" in text


@pytest.mark.asyncio
async def test_turn_active_suppresses_strip() -> None:
    """When ``turn_active`` is True, the workflow strip is hidden."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        status_bar = app.query_one("#status-bar", StatusBar)

        # Push a strip first so the widget is visible.
        status_bar.set_workflow_strip("▸ ref 1/3 · 5s")
        await pilot.pause()
        assert status_bar.has_class("-visible")

        # Now activate the turn — strip should suppress.
        status_bar.set_turn_active(True)
        await pilot.pause()

        assert not status_bar.has_class("-visible")

        # Clearing turn_active restores the strip.
        status_bar.set_turn_active(False)
        await pilot.pause()
        assert status_bar.has_class("-visible")


@pytest.mark.asyncio
async def test_terminal_workflow_hides_strip() -> None:
    """After a workflow flips terminal, the strip clears on the next flush."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)
        status_bar = app.query_one("#status-bar", StatusBar)

        card = _MockCard(id=CardId("crd_00000000-0000-0000-0000-000000000010"))
        record = _mk_record(
            suffix="b1",
            phase="running",
            card=card,
            started_offset=5,
        )
        widget_map: dict[Any, Any] = {}

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: [record],
            get_card_widget=lambda cid: widget_map.get(cid),
            reorder_tick=0.02,
            status_bar=status_bar,
        )

        # First flush: strip visible.
        await mgr._flush_reorder()
        await pilot.pause()
        assert status_bar.has_class("-visible")

        # Flip terminal.
        record.phase = "success"  # type: ignore[assignment]
        record.terminal_at = _NOW

        # Second flush after the phase change.
        await mgr._flush_reorder()
        await pilot.pause()

        assert not status_bar.has_class("-visible")
