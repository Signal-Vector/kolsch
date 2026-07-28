"""spec-016 Test 4 — ``--resume`` ordering (Pilot).

Two passes:

1. Session with 5 workflows — 3 completed (varying `terminal_at`),
   2 in-progress. Simulate chronological replay by mounting cards
   in creation order. After the layout flush, DOM order matches the
   canonical sort (not chronological).
2. Deterministic: two ``_flush_reorder`` passes on the same records
   produce byte-identical DOM child ordering.
"""

from __future__ import annotations

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
    created_offset: int = 0,
    started_offset: int | None = None,
    terminal_offset: int | None = None,
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=WorkflowRunId(f"wrk_00000000-0000-0000-0000-0000000000{suffix}"),
        ref="test/ref",
        phase=phase,
        card=card,
        created_at=_BASE_TS + timedelta(seconds=created_offset),
        started_at=(
            _BASE_TS + timedelta(seconds=started_offset)
            if started_offset is not None
            else None
        ),
        terminal_at=(
            _BASE_TS + timedelta(seconds=terminal_offset)
            if terminal_offset is not None
            else None
        ),
    )


@pytest.mark.asyncio
async def test_resume_produces_sorted_order_not_chronological() -> None:
    """After chronological mount + single flush, DOM matches canonical sort."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        cards = [
            _MockCard(id=CardId(f"crd_00000000-0000-0000-0000-000000002{i:03x}"))
            for i in range(5)
        ]
        widgets = [
            CardWidget(card_id=c.id, kind="workflow", title=f"W{i}")
            for i, c in enumerate(cards)
        ]
        # Mount in CHRONOLOGICAL order (as `--resume` would).
        for w in widgets:
            await transcript.mount(w)
        await pilot.pause()

        # 3 terminal successes (with varying terminal_at), 2 in-progress
        # (with varying started_at). Chronological mount = index order,
        # but canonical sort must differ.
        records = [
            _mk_record(
                suffix="c1",
                phase="success",
                card=cards[0],
                created_offset=1,
                terminal_offset=30,
            ),
            _mk_record(
                suffix="c2",
                phase="success",
                card=cards[1],
                created_offset=2,
                terminal_offset=10,
            ),
            _mk_record(
                suffix="c3",
                phase="running",
                card=cards[2],
                created_offset=3,
                started_offset=100,
            ),
            _mk_record(
                suffix="c4",
                phase="success",
                card=cards[3],
                created_offset=4,
                terminal_offset=20,
            ),
            _mk_record(
                suffix="c5",
                phase="running",
                card=cards[4],
                created_offset=5,
                started_offset=50,
            ),
        ]
        widget_map: dict[Any, Any] = {cards[i].id: widgets[i] for i in range(5)}

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        # Single flush at end of replay.
        await mgr._flush_reorder()
        await pilot.pause()

        expected = sort_workflow_cards(records)
        # Expected order:
        # terminal successes ASC terminal_at: c2 (10), c4 (20), c1 (30)
        # in-progress ASC started_at: c5 (50), c3 (100)
        expected_widgets = [widget_map[r.card.id] for r in expected]

        children = list(transcript.children)
        our_children = [c for c in children if c in widgets]
        assert our_children == expected_widgets, (
            f"expected {[w.title for w in expected_widgets]} got "
            f"{[w.title for w in our_children]}"
        )


@pytest.mark.asyncio
async def test_two_flush_passes_produce_identical_order() -> None:
    """Determinism: two flushes on the same records yield same DOM order."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        cards = [
            _MockCard(id=CardId(f"crd_00000000-0000-0000-0000-000000003{i:03x}"))
            for i in range(5)
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
                suffix="d1",
                phase="success",
                card=cards[0],
                terminal_offset=30,
            ),
            _mk_record(
                suffix="d2",
                phase="success",
                card=cards[1],
                terminal_offset=10,
            ),
            _mk_record(
                suffix="d3",
                phase="running",
                card=cards[2],
                started_offset=100,
            ),
            _mk_record(
                suffix="d4",
                phase="error",
                card=cards[3],
                terminal_offset=20,
            ),
            _mk_record(
                suffix="d5",
                phase="running",
                card=cards[4],
                started_offset=50,
            ),
        ]
        widget_map: dict[Any, Any] = {cards[i].id: widgets[i] for i in range(5)}

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()
        order_a = [c.title for c in transcript.children if c in widgets]
        divider_a = [getattr(c, "id", None) for c in transcript.children]

        # Second flush on the same records.
        await mgr._flush_reorder()
        await pilot.pause()
        order_b = [c.title for c in transcript.children if c in widgets]
        divider_b = [getattr(c, "id", None) for c in transcript.children]
        assert order_a == order_b
        assert divider_a == divider_b
