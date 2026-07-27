"""spec-016 Test 2 — `WorkflowLayoutManager` reorder batching (Pilot).

Four passes:

1. Boot a host app with three CardWidgets A, B, C — all in-progress.
   Initial DOM order [A, B, C].
2. Terminal A (success). `notify_card_state_changed(A)`. After the
   250ms tick, DOM order [A_terminal, divider, B, C].
3. Terminal B (error) — pinned at boundary. Order becomes
   [A, B, divider, C] (B at bottom of terminal band, unacked).
4. Simulate ack of B (Enter binding fires). After tick,
   order [A, B, divider, C] with both A and B acked/terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from cli.chat.services.workflow_layout import (
    DIVIDER_ID,
    WorkflowLayoutManager,
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


class _TranscriptApp(App):
    """Host app exposing a single ``#transcript`` VerticalScroll."""

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")


def _mk_record(
    *,
    run_id_suffix: str,
    phase: str,
    card: _MockCard,
    started_offset: int = 0,
    terminal_offset: int | None = None,
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=WorkflowRunId(f"wrk_00000000-0000-0000-0000-0000000000{run_id_suffix}"),
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


@pytest.mark.asyncio
async def test_three_in_progress_no_divider() -> None:
    """Initial state: three in-progress cards, no divider mounted."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        card_a = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000000a"))
        card_b = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000000b"))
        card_c = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000000c"))

        widget_a = CardWidget(card_id=card_a.id, kind="workflow", title="A")
        widget_b = CardWidget(card_id=card_b.id, kind="workflow", title="B")
        widget_c = CardWidget(card_id=card_c.id, kind="workflow", title="C")
        await transcript.mount(widget_a)
        await transcript.mount(widget_b)
        await transcript.mount(widget_c)
        await pilot.pause()

        rec_a = _mk_record(
            run_id_suffix="01", phase="running", card=card_a, started_offset=1
        )
        rec_b = _mk_record(
            run_id_suffix="02", phase="running", card=card_b, started_offset=2
        )
        rec_c = _mk_record(
            run_id_suffix="03", phase="running", card=card_c, started_offset=3
        )
        records = [rec_a, rec_b, rec_c]
        widgets: dict[Any, Any] = {
            card_a.id: widget_a,
            card_b.id: widget_b,
            card_c.id: widget_c,
        }

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widgets.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # No divider mounted (only in-progress group).
        try:
            transcript.query_one(f"#{DIVIDER_ID}", Static)
            has_divider = True
        except Exception:
            has_divider = False
        assert not has_divider, "divider should not mount with only in-progress"

        # DOM order [A, B, C] preserved.
        children = list(transcript.children)
        assert children[:3] == [widget_a, widget_b, widget_c]


@pytest.mark.asyncio
async def test_terminal_success_moves_to_top_and_divider_mounts() -> None:
    """A goes terminal → [A_terminal, divider, B, C]."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        card_a = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000001a"))
        card_b = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000001b"))
        card_c = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000001c"))

        widget_a = CardWidget(card_id=card_a.id, kind="workflow", title="A")
        widget_b = CardWidget(card_id=card_b.id, kind="workflow", title="B")
        widget_c = CardWidget(card_id=card_c.id, kind="workflow", title="C")
        await transcript.mount(widget_a)
        await transcript.mount(widget_b)
        await transcript.mount(widget_c)
        await pilot.pause()

        # A is terminal (success); B and C still running.
        rec_a = _mk_record(
            run_id_suffix="11",
            phase="success",
            card=card_a,
            terminal_offset=10,
        )
        rec_b = _mk_record(
            run_id_suffix="12", phase="running", card=card_b, started_offset=2
        )
        rec_c = _mk_record(
            run_id_suffix="13", phase="running", card=card_c, started_offset=3
        )
        records = [rec_a, rec_b, rec_c]
        widgets: dict[Any, Any] = {
            card_a.id: widget_a,
            card_b.id: widget_b,
            card_c.id: widget_c,
        }

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widgets.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # Divider should be mounted between terminal (A) and in-progress (B, C).
        divider = transcript.query_one(f"#{DIVIDER_ID}", Static)
        children = list(transcript.children)
        idx_a = children.index(widget_a)
        idx_div = children.index(divider)
        idx_b = children.index(widget_b)
        idx_c = children.index(widget_c)
        assert idx_a < idx_div < idx_b < idx_c, (
            f"expected A,divider,B,C but got indices {idx_a},{idx_div},{idx_b},{idx_c}"
        )


@pytest.mark.asyncio
async def test_terminal_error_pins_at_boundary() -> None:
    """A success, B error unacked, C running → [A, B, divider, C]."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        card_a = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000002a"))
        card_b = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000002b"))
        card_c = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000002c"))

        widget_a = CardWidget(card_id=card_a.id, kind="workflow", title="A")
        widget_b = CardWidget(card_id=card_b.id, kind="workflow", title="B")
        widget_c = CardWidget(card_id=card_c.id, kind="workflow", title="C")
        await transcript.mount(widget_a)
        await transcript.mount(widget_b)
        await transcript.mount(widget_c)
        await pilot.pause()

        rec_a = _mk_record(
            run_id_suffix="21",
            phase="success",
            card=card_a,
            terminal_offset=10,
        )
        rec_b = _mk_record(
            run_id_suffix="22",
            phase="error",
            card=card_b,
            terminal_offset=20,
        )
        rec_c = _mk_record(
            run_id_suffix="23",
            phase="running",
            card=card_c,
            started_offset=3,
        )
        records = [rec_a, rec_b, rec_c]
        widgets: dict[Any, Any] = {
            card_a.id: widget_a,
            card_b.id: widget_b,
            card_c.id: widget_c,
        }

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widgets.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        divider = transcript.query_one(f"#{DIVIDER_ID}", Static)
        children = list(transcript.children)
        idx_a = children.index(widget_a)
        idx_b = children.index(widget_b)
        idx_div = children.index(divider)
        idx_c = children.index(widget_c)
        # A first (terminal success), B pinned at boundary (unacked
        # failure), divider, then C in-progress.
        assert idx_a < idx_b < idx_div < idx_c, (
            f"expected A,B,divider,C but got indices {idx_a},{idx_b},{idx_div},{idx_c}"
        )


@pytest.mark.asyncio
async def test_ack_error_moves_into_terminal_band() -> None:
    """B ack fires; B moves before divider into terminal group."""
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        card_a = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000003a"))
        card_b = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000003b"))
        card_c = _MockCard(id=CardId("crd_00000000-0000-0000-0000-00000000003c"))

        widget_a = CardWidget(card_id=card_a.id, kind="workflow", title="A")
        widget_b = CardWidget(card_id=card_b.id, kind="workflow", title="B")
        widget_c = CardWidget(card_id=card_c.id, kind="workflow", title="C")
        await transcript.mount(widget_a)
        await transcript.mount(widget_b)
        await transcript.mount(widget_c)
        await pilot.pause()

        rec_a = _mk_record(
            run_id_suffix="31",
            phase="success",
            card=card_a,
            terminal_offset=10,
        )
        rec_b = _mk_record(
            run_id_suffix="32",
            phase="error",
            card=card_b,
            terminal_offset=20,
        )
        rec_c = _mk_record(
            run_id_suffix="33",
            phase="running",
            card=card_c,
            started_offset=3,
        )
        records = [rec_a, rec_b, rec_c]
        widgets: dict[Any, Any] = {
            card_a.id: widget_a,
            card_b.id: widget_b,
            card_c.id: widget_c,
        }

        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widgets.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # Widget B should now have an ack callback wired.
        assert widget_b._on_error_ack is not None

        # Fire the ack (simulates Enter binding).
        # qol-084: widget-internal state uses canonical `failed`
        # (wire `error` is only accepted via apply_close, which
        # translates).
        widget_b.state = "failed"
        widget_b.action_expand_or_ack()
        await pilot.pause()

        # Ack sets record.acked True.
        assert rec_b.acked is True

        # Wait for the scheduled reorder to fire.
        import asyncio as _asyncio

        await _asyncio.sleep(0.05)
        await pilot.pause()

        # After ack, B joins terminal band. Order should now be
        # [B (terminal by terminal_at=20 > A=10 -> A first), divider, C].
        divider = transcript.query_one(f"#{DIVIDER_ID}", Static)
        children = list(transcript.children)
        idx_a = children.index(widget_a)
        idx_b = children.index(widget_b)
        idx_div = children.index(divider)
        idx_c = children.index(widget_c)
        # A terminal_at=10, B terminal_at=20, so both in terminal band
        # with A before B; divider then C.
        assert idx_a < idx_b < idx_div < idx_c, (
            f"expected A,B,divider,C (both terminal) but got "
            f"{idx_a},{idx_b},{idx_div},{idx_c}"
        )
