"""bug-039 — user can't expand workflow rows after workflow completes.

Reproduction + regression tests for the case-a failure described in
`process/bug-039-cannot-expand-workflow-after-completion.md`.

The user completes a workflow and then tries to expand `WorkflowNodeRow`
widgets inside the terminal card. Before the fix the whole node list
is `display=False` because spec-016 flips terminal cards into compact
mode unconditionally, hiding the `WorkflowNodeList` container. After
the fix the node list stays visible on a terminal card unless space
pressure demands otherwise (multiple in-progress workflows co-existing).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Collapsible

from cli.chat.services.workflow_layout import WorkflowLayoutManager
from cli.chat.services.workflows_impl import WorkflowRunRecord
from cli.chat.widgets.assistant_prose import AssistantProse
from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.workflow_node_row import WorkflowNodeRow
from schemas.identifiers import CardId
from workflows.ids import WorkflowRunId

_BASE_TS = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


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


async def _setup(pilot, n: int) -> tuple[Any, list[Any], list[Any]]:
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
async def test_solo_terminal_card_keeps_node_list_visible() -> None:
    """Regression: a lone terminal card must not be forced compact.

    User workflow: run ONE workflow, wait for completion, expand a
    row. Before the fix `compact_mode` was set True unconditionally on
    terminal-phase records — the `WorkflowNodeList` container got
    `display=False` and the row was unreachable.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup(pilot, 1)

        records = [
            _mk_record(
                suffix="01",
                phase="success",
                card=cards[0],
                terminal_offset=5,
            ),
        ]
        widget_map: dict[Any, Any] = {cards[0].id: widgets[0]}
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        renderer = widgets[0]._body
        # Fix: lone terminal card is NOT compact.
        assert renderer.compact_mode is False, (
            "solo terminal card must stay full-render so the user can "
            "still expand its node rows post-completion"
        )
        assert renderer._node_list.display is True


@pytest.mark.asyncio
async def test_terminal_row_expand_after_completion_drains_buffer() -> None:
    """End-to-end: buffered events replay on post-completion expand.

    Mounts a real `WorkflowNodeRow` inside a card; buffers a sub-agent
    assistant message while collapsed; transitions the row to
    `completed`; then flushes the layout manager as if the workflow
    just terminated. Expanding the row after completion must still
    surface the buffered widget.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup(pilot, 1)

        # Mount a node row inside the workflow renderer.
        renderer = widgets[0]._body
        row = WorkflowNodeRow(node_name="n")
        # bug-095: _node_rows is composite-keyed (name, invocation_id).
        renderer._node_rows[("n", "")] = row
        renderer._node_list.add_node(row)
        await pilot.pause()

        # Buffer a sub-agent event while collapsed.
        await row.mount_assistant_message("hello", event_id="ev-a")
        await pilot.pause()
        assert len(row._pending_mounts) == 1

        # Terminal state on the row — buffer freezes, but the queue
        # survives (spec-021 replay-on-expand invariant).
        row.set_state("success", details="done")
        await pilot.pause()

        # Layout manager runs its terminal reorder pass.
        records = [
            _mk_record(
                suffix="01",
                phase="success",
                card=cards[0],
                terminal_offset=5,
            ),
        ]
        widget_map: dict[Any, Any] = {cards[0].id: widgets[0]}
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # Fix invariant: the node list is still displayed.
        assert renderer._node_list.display is True

        # User expands the row after completion.
        row.collapsed = False
        await pilot.pause()
        await pilot.pause()

        # The buffered assistant message replayed.
        contents = row.query_one(Collapsible.Contents)
        prose = list(contents.query(AssistantProse))
        assert len(prose) == 1


@pytest.mark.asyncio
async def test_multi_in_progress_still_compacts_terminal_card() -> None:
    """When space pressure hits (>2 in-progress), terminal card still compacts.

    spec-016's original policy is preserved when there really is a
    space budget to defend: three concurrent in-progress plus a
    terminal card → all four go compact. Only the "lone terminal card
    with no space pressure" case flips to full-render.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup(pilot, 4)

        records = [
            _mk_record(
                suffix="01",
                phase="success",
                card=cards[0],
                terminal_offset=5,
            ),
            _mk_record(suffix="02", phase="running", card=cards[1], started_offset=1),
            _mk_record(suffix="03", phase="running", card=cards[2], started_offset=2),
            _mk_record(suffix="04", phase="running", card=cards[3], started_offset=3),
        ]
        widget_map: dict[Any, Any] = {cards[i].id: widgets[i] for i in range(4)}
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # 3 in-progress -> compact-mode threshold met. Terminal card
        # stays compact under space pressure.
        assert widgets[0]._body.compact_mode is True
        assert widgets[1]._body.compact_mode is True
        assert widgets[2]._body.compact_mode is True
        assert widgets[3]._body.compact_mode is True


@pytest.mark.asyncio
async def test_terminal_card_with_one_in_progress_stays_full() -> None:
    """Terminal card + 1 in-progress → both full render.

    Two cards fit on-screen; there's no space pressure. The user
    should be able to expand rows on the terminal card while the
    other card streams.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup(pilot, 2)

        records = [
            _mk_record(
                suffix="01",
                phase="success",
                card=cards[0],
                terminal_offset=5,
            ),
            _mk_record(
                suffix="02",
                phase="running",
                card=cards[1],
                started_offset=1,
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

        assert widgets[0]._body.compact_mode is False
        assert widgets[1]._body.compact_mode is False
