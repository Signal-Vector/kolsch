"""spec-016 Test 3 — Compact-mode transitions (Pilot).

Five passes:

1. 1 workflow → full render (compact_mode False).
2. 2 workflows → both full.
3. 3 workflows → all three compact.
4. Terminal transition → moved card ALWAYS compact.
5. Down to 2 in-progress → back to full render.
   CSS class ``.-compact`` on ``WorkflowRenderer`` when in compact mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.services.workflow_layout import WorkflowLayoutManager
from cli.chat.services.workflows_impl import WorkflowRunRecord
from cli.chat.widgets.card_widget import CardWidget
from schemas.identifiers import CardId
from workflows.ids import WorkflowRunId

_BASE_TS = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


@dataclass
class _MockCard:
    id: CardId
    is_closed: bool = False


class _TranscriptApp(App):
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
        phase=phase,  # type: ignore[arg-type]
        card=card,  # type: ignore[arg-type]
        created_at=_BASE_TS,
        started_at=_BASE_TS + timedelta(seconds=started_offset),
        terminal_at=(
            _BASE_TS + timedelta(seconds=terminal_offset)
            if terminal_offset is not None
            else None
        ),
    )


async def _setup_cards(pilot, n: int) -> tuple[Any, list[Any], list[Any]]:
    """Return (transcript, widgets, cards) with `n` mounted workflow cards."""
    transcript = pilot.app.query_one("#transcript", VerticalScroll)
    widgets = []
    cards = []
    for i in range(n):
        cid = CardId(f"crd_00000000-0000-0000-0000-0000000000{i:02x}")
        card = _MockCard(id=cid)
        widget = CardWidget(card_id=cid, kind="workflow", title=f"W{i}")
        await transcript.mount(widget)
        widgets.append(widget)
        cards.append(card)
    await pilot.pause()
    return transcript, widgets, cards


@pytest.mark.asyncio
async def test_one_workflow_full_render() -> None:
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 1)

        rec = _mk_record(suffix="01", phase="running", card=cards[0], started_offset=1)
        records = [rec]
        widget_map: dict[Any, Any] = {cards[0].id: widgets[0]}

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()
        assert widgets[0]._body.compact_mode is False


@pytest.mark.asyncio
async def test_two_workflows_full_render() -> None:
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 2)

        records = [
            _mk_record(suffix="01", phase="running", card=cards[0], started_offset=1),
            _mk_record(suffix="02", phase="running", card=cards[1], started_offset=2),
        ]
        widget_map: dict[Any, Any] = {
            cards[0].id: widgets[0],
            cards[1].id: widgets[1],
        }
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        assert widgets[0]._body.compact_mode is False
        assert widgets[1]._body.compact_mode is False


@pytest.mark.asyncio
async def test_three_workflows_all_compact() -> None:
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 3)

        records = [
            _mk_record(suffix="01", phase="running", card=cards[0], started_offset=1),
            _mk_record(suffix="02", phase="running", card=cards[1], started_offset=2),
            _mk_record(suffix="03", phase="running", card=cards[2], started_offset=3),
        ]
        widget_map: dict[Any, Any] = {
            cards[0].id: widgets[0],
            cards[1].id: widgets[1],
            cards[2].id: widgets[2],
        }
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # All three should be compact (none has focus).
        assert widgets[0]._body.compact_mode is True
        assert widgets[1]._body.compact_mode is True
        assert widgets[2]._body.compact_mode is True

        # CSS class `.-compact` applied on each renderer.
        assert widgets[0]._body.has_class("-compact")
        assert widgets[1]._body.has_class("-compact")
        assert widgets[2]._body.has_class("-compact")


@pytest.mark.asyncio
async def test_lone_terminal_and_one_in_progress_both_full() -> None:
    """bug-039: lone terminal card stays full-render so its rows expand.

    Pre-bug-039 policy: terminal cards were ALWAYS compact — the
    ``WorkflowNodeList`` container went ``display=False`` and the
    user could not click into node rows to inspect what completed.
    Post-fix policy: a lone terminal card (single terminal, no
    in-progress space pressure) stays full-render. Multiple terminals
    still compact so accumulated history stays legible — see
    ``test_multiple_terminal_cards_go_compact``.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 2)

        records = [
            _mk_record(
                suffix="01",
                phase="success",
                card=cards[0],
                terminal_offset=5,
            ),
            _mk_record(suffix="02", phase="running", card=cards[1], started_offset=1),
        ]
        widget_map: dict[Any, Any] = {
            cards[0].id: widgets[0],
            cards[1].id: widgets[1],
        }
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # No space pressure (1 in-progress, 1 terminal) → both full.
        assert widgets[0]._body.compact_mode is False
        assert widgets[1]._body.compact_mode is False


@pytest.mark.asyncio
async def test_multiple_terminal_cards_go_compact() -> None:
    """bug-039: 2+ terminal cards compact so accumulated history stays terse."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 2)

        records = [
            _mk_record(
                suffix="01",
                phase="success",
                card=cards[0],
                terminal_offset=5,
            ),
            _mk_record(
                suffix="02",
                phase="success",
                card=cards[1],
                terminal_offset=6,
            ),
        ]
        widget_map: dict[Any, Any] = {
            cards[0].id: widgets[0],
            cards[1].id: widgets[1],
        }
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # 2 terminals → both compact.
        assert widgets[0]._body.compact_mode is True
        assert widgets[1]._body.compact_mode is True


@pytest.mark.asyncio
async def test_drop_from_three_to_two_returns_full() -> None:
    """3 in-progress → 2 in-progress → both in-progress back to full."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 3)

        records = [
            _mk_record(suffix="01", phase="running", card=cards[0], started_offset=1),
            _mk_record(suffix="02", phase="running", card=cards[1], started_offset=2),
            _mk_record(suffix="03", phase="running", card=cards[2], started_offset=3),
        ]
        widget_map: dict[Any, Any] = {
            cards[0].id: widgets[0],
            cards[1].id: widgets[1],
            cards[2].id: widgets[2],
        }
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # All three compact.
        assert widgets[0]._body.compact_mode is True
        assert widgets[1]._body.compact_mode is True
        assert widgets[2]._body.compact_mode is True

        # Now record 2 goes terminal.
        records[2] = _mk_record(
            suffix="03",
            phase="success",
            card=cards[2],
            terminal_offset=10,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # In-progress count now 2; those two should be FULL render again.
        assert widgets[0]._body.compact_mode is False
        assert widgets[1]._body.compact_mode is False
        # bug-039: with only 1 terminal card and no in-progress space
        # pressure, the terminal card also stays full-render so the
        # user can drill into its rows.
        assert widgets[2]._body.compact_mode is False
        # CSS class removed on all three renderers.
        assert not widgets[0]._body.has_class("-compact")
        assert not widgets[1]._body.has_class("-compact")
        assert not widgets[2]._body.has_class("-compact")
