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

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.services.workflow_layout import DIVIDER_ID, WorkflowLayoutManager
from cli.chat.services.workflows_impl import WorkflowRunRecord
from cli.chat.widgets.card_widget import CardWidget
from schemas.identifiers import CardId
from workflows.ids import WorkflowRunId

if TYPE_CHECKING:
    from textual.pilot import Pilot

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


async def _setup_cards(pilot: Pilot[None], n: int) -> tuple[Any, list[Any], list[Any]]:
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
        # mypy pins `.compact_mode` to Literal[True] from the earlier
        # `is True` asserts and cannot see the reactive mutate at runtime,
        # so it reads the flipped-back checks as unreachable.
        assert widgets[0]._body.compact_mode is False
        assert widgets[1]._body.compact_mode is False  # type: ignore[unreachable]  # reactive flipped back to False at runtime
        # bug-039: with only 1 terminal card and no in-progress space
        # pressure, the terminal card also stays full-render so the
        # user can drill into its rows.
        assert widgets[2]._body.compact_mode is False
        # CSS class removed on all three renderers.
        assert not widgets[0]._body.has_class("-compact")
        assert not widgets[1]._body.has_class("-compact")
        assert not widgets[2]._body.has_class("-compact")


@pytest.mark.asyncio
async def test_bug115_cleared_terminal_cards_do_not_compact_fresh_card() -> None:
    """bug-115: a lone card started after ``/clear`` renders full even when
    the cleared session's terminal records still sit in the registry.

    Reproduces the leak: two finished workflows make ``len(terminal) > 1``
    (both compact). ``/clear`` unmounts their cards but leaves the records
    (and the never-pruned ``_card_widgets`` entries). Before the fix a
    third, lone workflow started next inherits the stale overload and
    renders compact; after the fix the unmounted ghosts drop out of the
    count and the fresh card renders full.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 3)

        # Two finished workflows → both compact (baseline / leak source).
        records = [
            _mk_record(suffix="01", phase="success", card=cards[0], terminal_offset=5),
            _mk_record(suffix="02", phase="success", card=cards[1], terminal_offset=6),
        ]
        # `_card_widgets` is never pruned on unmount, so ALL three ids stay
        # resolvable — exactly the production condition the filter handles.
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
        assert widgets[0]._body.compact_mode is True
        assert widgets[1]._body.compact_mode is True

        # `/clear`: unmount the two old cards (records + widget_map linger).
        await widgets[0].remove()
        await widgets[1].remove()
        await pilot.pause()

        # A fresh workflow starts in the "new" session and finishes.
        records.append(
            _mk_record(suffix="03", phase="success", card=cards[2], terminal_offset=7),
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # Only ONE mounted terminal card now → no overload → full render.
        assert widgets[2]._body.compact_mode is False
        assert not widgets[2]._body.has_class("-compact")


@pytest.mark.asyncio
async def test_bug115_flush_does_not_mutate_source_records() -> None:
    """bug-115: the mounted-ness filter operates on a local copy — the
    caller's record collection (the live run registry) is never mutated,
    preserving ``/clear``'s display-only contract.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 2)
        records = [
            _mk_record(suffix="01", phase="success", card=cards[0], terminal_offset=5),
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
        snapshot = list(records)
        # Unmount one card so the filter actually drops a record this pass.
        await widgets[0].remove()
        await pilot.pause()
        await mgr._flush_reorder()
        await pilot.pause()
        # Source list untouched: same objects, order, and length.
        assert records == snapshot
        assert len(records) == 2


@pytest.mark.asyncio
async def test_bug115_reset_drops_divider_and_pending_flag() -> None:
    """bug-115: ``reset()`` clears the dangling divider ref + pending flag
    so a ``/clear`` leaves no stale layout state behind.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 2)
        # Dual group (1 terminal + 1 in-progress) → the divider mounts.
        records = [
            _mk_record(suffix="01", phase="success", card=cards[0], terminal_offset=5),
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
        divider = mgr._divider
        assert divider is not None
        mgr._pending_reorder = True

        mgr.reset()

        assert mgr._divider is None
        assert mgr._pending_reorder is False


@pytest.mark.asyncio
async def test_bug115_cleared_in_progress_cards_do_not_compact_fresh_card() -> None:
    """bug-115: the in-progress half of the leak (``len(in_progress) > 2``).

    Clearing WHILE workflows are in flight leaves 3+ stale ``running``
    records in the registry; before the fix a fresh lone running card in
    the "new" session inherits that overload and renders compact. The
    mounted-ness filter drops the detached ghosts so the fresh card is the
    only in-progress card → full render. (The terminal half is covered by
    ``test_bug115_cleared_terminal_cards_do_not_compact_fresh_card``.)
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 4)

        # Three concurrent in-progress workflows → all compact (leak source).
        records = [
            _mk_record(suffix="01", phase="running", card=cards[0], started_offset=1),
            _mk_record(suffix="02", phase="running", card=cards[1], started_offset=2),
            _mk_record(suffix="03", phase="running", card=cards[2], started_offset=3),
        ]
        widget_map: dict[Any, Any] = {
            cards[0].id: widgets[0],
            cards[1].id: widgets[1],
            cards[2].id: widgets[2],
            cards[3].id: widgets[3],
        }
        mgr = WorkflowLayoutManager(
            transcript=transcript,
            get_records=lambda: records,
            get_card_widget=widget_map.get,
            reorder_tick=0.01,
        )
        await mgr._flush_reorder()
        await pilot.pause()
        assert widgets[0]._body.compact_mode is True

        # `/clear` mid-flight: unmount the three in-progress cards.
        await widgets[0].remove()
        await widgets[1].remove()
        await widgets[2].remove()
        await pilot.pause()

        # A fresh workflow starts running in the "new" session.
        records.append(
            _mk_record(suffix="04", phase="running", card=cards[3], started_offset=4),
        )
        await mgr._flush_reorder()
        await pilot.pause()

        # Only ONE mounted in-progress card → no overload → full render.
        assert widgets[3]._body.compact_mode is False
        assert not widgets[3]._body.has_class("-compact")


def test_bug115_card_is_mounted_counts_on_ambiguous_state() -> None:
    """bug-115 fail-safe: ``_card_is_mounted`` never DROPS on ambiguity.

    Only a widget that is known AND detached is dropped. A record with no
    card, a card whose widget isn't in the map yet (brand-new, mid-mount),
    or a widget whose ``is_attached`` property raises must all COUNT
    (return True) so a genuine overload is never under-counted into a
    wrong full render. Pure unit test — no running app needed.
    """

    class _RaisingWidget:
        @property
        def is_attached(self) -> bool:
            raise RuntimeError("boom")

    card_detached = _MockCard(id=CardId("crd_00000000-0000-0000-0000-0000000000d0"))
    card_attached = _MockCard(id=CardId("crd_00000000-0000-0000-0000-0000000000a0"))
    card_raising = _MockCard(id=CardId("crd_00000000-0000-0000-0000-0000000000e0"))
    card_unmapped = _MockCard(id=CardId("crd_00000000-0000-0000-0000-0000000000f0"))

    widget_map: dict[Any, Any] = {
        card_detached.id: SimpleNamespace(is_attached=False),
        card_attached.id: SimpleNamespace(is_attached=True),
        card_raising.id: _RaisingWidget(),
    }
    mgr = WorkflowLayoutManager(
        transcript=cast("Any", None),
        get_records=list,  # unused here — `_card_is_mounted` is called directly
        get_card_widget=widget_map.get,
        reorder_tick=0.01,
    )

    rec_det = _mk_record(suffix="d0", phase="running", card=card_detached)
    rec_att = _mk_record(suffix="a0", phase="running", card=card_attached)
    rec_rai = _mk_record(suffix="e0", phase="running", card=card_raising)
    rec_unmapped = _mk_record(suffix="f0", phase="running", card=card_unmapped)
    rec_no_card = replace(rec_att, card=None)

    # The ONLY drop: known widget that is explicitly detached.
    assert mgr._card_is_mounted(rec_det) is False
    # Everything else counts (True) — the never-under-count guarantee.
    assert mgr._card_is_mounted(rec_att) is True
    assert mgr._card_is_mounted(rec_rai) is True  # property raised → fail-safe
    assert mgr._card_is_mounted(rec_unmapped) is True  # not in widget map yet
    assert mgr._card_is_mounted(rec_no_card) is True  # no card bound


@pytest.mark.asyncio
async def test_bug115_divider_remounts_cleanly_after_reset() -> None:
    """bug-115: the next dual-group flush after ``reset()`` re-mounts a
    single FRESH divider.

    Proves ``reset()``'s divider nil-out is load-bearing: without it the
    post-clear flush would take ``_sync_divider``'s reposition branch,
    hit ``children.index(dead_static)`` → ``ValueError``, and the divider
    would never re-mount for the new session.
    """
    app = _TranscriptApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript, widgets, cards = await _setup_cards(pilot, 2)
        # Dual group (1 terminal + 1 in-progress) mounts the divider.
        records = [
            _mk_record(suffix="01", phase="success", card=cards[0], terminal_offset=5),
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
        first_divider = mgr._divider
        assert first_divider is not None

        # Simulate `/clear`: unmount every child (incl. the divider) + reset.
        for child in list(transcript.children):
            await child.remove()
        await pilot.pause()
        mgr.reset()
        after_reset = mgr._divider
        assert after_reset is None

        # New dual-group session mounts into the same #transcript.
        _t2, widgets2, cards2 = await _setup_cards(pilot, 2)
        records.clear()
        records.append(
            _mk_record(suffix="03", phase="success", card=cards2[0], terminal_offset=5),
        )
        records.append(
            _mk_record(suffix="04", phase="running", card=cards2[1], started_offset=1),
        )
        widget_map[cards2[0].id] = widgets2[0]
        widget_map[cards2[1].id] = widgets2[1]
        await mgr._flush_reorder()
        await pilot.pause()

        # A fresh divider re-mounted — not the dead one — and exactly one.
        final_divider = mgr._divider
        assert final_divider is not None
        assert final_divider is not first_divider
        dividers = [
            c for c in transcript.children if getattr(c, "id", None) == DIVIDER_ID
        ]
        assert len(dividers) == 1
