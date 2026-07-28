"""spec-016 Test 5 — Simultaneous terminals → single reorder pass.

Two passes:

1. Three workflows in flight. Terminal all three within 100ms of
   each other. Verify (via ``flush_count``) that ``_flush_reorder``
   runs ONCE (not three times) within the 250ms tick window.
2. Final DOM order matches ``sort_workflow_cards`` result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.services.workflow_layout import (
    WorkflowLayoutManager,
    sort_workflow_cards,
)
from cli.chat.services.workflows_impl import WorkflowRunRecord
from cli.chat.widgets.card_widget import CardWidget
from schemas.identifiers import CardId
from workflows.ids import WorkflowRunId

_BASE_TS = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


@dataclass
class _MockCard:
    id: CardId
    is_closed: bool = False


class _TranscriptApp(App[None]):
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")


def _mk_record(
    *,
    suffix: str,
    phase: str,
    card: _MockCard,
    started_offset: int = 0,
    terminal_offset: int | None = None,
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=WorkflowRunId(f"wrk_00000000-0000-0000-0000-0000000000{suffix}"),
        ref="test/ref",
        phase=phase,
        card=card,
        created_at=_BASE_TS,
        started_at=_BASE_TS + timedelta(seconds=started_offset),
        terminal_at=(
            _BASE_TS + timedelta(seconds=terminal_offset)
            if terminal_offset is not None
            else None
        ),
    )


@pytest.mark.asyncio
async def test_three_simultaneous_notifies_fire_one_flush() -> None:
    """Three notifies within tick window collapse to one flush."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        cards = [
            _MockCard(id=CardId(f"crd_00000000-0000-0000-0000-000000000{i:03x}"))
            for i in range(3)
        ]
        widgets = [
            CardWidget(card_id=c.id, kind="workflow", title=f"W{i}")
            for i, c in enumerate(cards)
        ]
        for w in widgets:
            await transcript.mount(w)
        await pilot.pause()

        records = [
            _mk_record(
                suffix=f"a{i}",
                phase="running",
                card=cards[i],
                started_offset=i,
            )
            for i in range(3)
        ]
        widget_map: dict[Any, Any] = {cards[i].id: widgets[i] for i in range(3)}

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.10,
        )

        # Three rapid notifies within the tick window.
        mgr.notify_card_state_changed(records[0].run_id)
        mgr.notify_card_state_changed(records[1].run_id)
        mgr.notify_card_state_changed(records[2].run_id)

        # Wait past the tick.
        await asyncio.sleep(0.15)
        await pilot.pause()

        # Only ONE flush should have run.
        assert mgr.flush_count == 1, f"expected 1 flush, got {mgr.flush_count}"


@pytest.mark.asyncio
async def test_final_dom_order_matches_sort() -> None:
    """After batch flush, DOM order matches ``sort_workflow_cards`` result."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        cards = [
            _MockCard(id=CardId(f"crd_00000000-0000-0000-0000-000000001{i:03x}"))
            for i in range(3)
        ]
        widgets = [
            CardWidget(card_id=c.id, kind="workflow", title=f"W{i}")
            for i, c in enumerate(cards)
        ]
        # Mount in DELIBERATELY REVERSED order so we can see the sort
        # move them.
        for w in reversed(widgets):
            await transcript.mount(w)
        await pilot.pause()

        # All three terminal successes with different `terminal_at`.
        records = [
            _mk_record(
                suffix="b1",
                phase="success",
                card=cards[0],
                terminal_offset=30,
            ),
            _mk_record(
                suffix="b2",
                phase="success",
                card=cards[1],
                terminal_offset=10,
            ),
            _mk_record(
                suffix="b3",
                phase="success",
                card=cards[2],
                terminal_offset=20,
            ),
        ]
        widget_map: dict[Any, Any] = {cards[i].id: widgets[i] for i in range(3)}

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.05,
        )

        # Three simultaneous notifies.
        mgr.notify_card_state_changed(records[0].run_id)
        mgr.notify_card_state_changed(records[1].run_id)
        mgr.notify_card_state_changed(records[2].run_id)

        await asyncio.sleep(0.10)
        await pilot.pause()

        # Sort by terminal_at ASC: b2 (10), b3 (20), b1 (30).
        expected_order = sort_workflow_cards(records)
        expected_widgets = [widget_map[r.card.id] for r in expected_order]

        children = list(transcript.children)
        # Filter to just our card widgets (skip divider if any).
        our_children = [c for c in children if c in widgets]
        assert our_children == expected_widgets, (
            f"expected {expected_widgets} but got {our_children}"
        )
