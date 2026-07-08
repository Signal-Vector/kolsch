"""feat-019 Test 4 — `x` force-kill binding + `ForceKillConfirm` modal.

Five passes covering the guard rails and the modal's y/n dispatch:

1. Focused ``cancelling`` card with time-in-state > 5s → ``x``
   mounts :class:`ForceKillConfirm`.
2. Focused ``cancelling`` card with time-in-state < 5s → ``x`` is a
   no-op.
3. Focused ``running`` card → ``x`` is a no-op.
4. :class:`ForceKillConfirm` on ``y`` → calls
   ``Workflows.cancel(run_id, cascade=True)``.
5. :class:`ForceKillConfirm` on ``n`` → modal unmounts; no cancel.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from cli.chat.widgets.card_widget import CardWidget
from cli.chat.widgets.force_kill_confirm import ForceKillConfirm
from schemas.identifiers import CardId
from workflows.ids import WorkflowRunId


class _StubWorkflowsImpl:
    """Bare-bones stand-in for :class:`WorkflowsImpl`."""

    def __init__(self) -> None:
        self.cancel_calls: list[tuple[Any, bool]] = []
        # Fake cascade tree: run_id plus 2 descendants.
        self._tree_size = 3

    def _collect_cancel_tree(self, run_id: Any) -> list[Any]:
        return [run_id] + [f"child_{i}" for i in range(self._tree_size - 1)]

    async def cancel(self, run_id: Any, *, cascade: bool = False) -> bool:
        self.cancel_calls.append((run_id, cascade))
        return True


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")


def _make_card_with_state(
    *, state: str, seconds_in_state: float,
) -> CardWidget:
    """Build a workflow-kind CardWidget already in ``state``.

    Sets ``_state_entered_at`` to a past monotonic timestamp so
    :meth:`CardWidget._time_in_state` returns ``seconds_in_state``
    without waiting real time.
    """
    card = CardWidget(
        card_id=CardId("crd_00000000-0000-0000-0000-000000000001"),
        kind="workflow",
        title="test-workflow",
    )
    # Force the reactive state so ``watch_state`` sets the entry clock.
    card.state = state
    # Then rewind the clock by ``seconds_in_state`` so the guard reads
    # the desired residency.
    card._state_entered_at = time.monotonic() - seconds_in_state
    return card


@pytest.mark.asyncio
async def test_force_kill_mounts_when_guards_pass() -> None:
    """Focused cancelling card with >5s time-in-state → modal mounts."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        card = _make_card_with_state(state="cancelling", seconds_in_state=6.0)
        stub = _StubWorkflowsImpl()
        run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-000000000001")
        card.bind_workflow(workflows_impl=stub, run_id=run_id)

        await transcript.mount(card)
        await pilot.pause()

        card.action_force_kill()
        await pilot.pause()

        # ForceKillConfirm should be mounted somewhere in the app.
        modals = list(app.query(ForceKillConfirm))
        assert len(modals) == 1
        # Question mentions the correct N related (tree size 3 - 1 = 2).
        assert modals[0]._n_related == 2


@pytest.mark.asyncio
async def test_force_kill_no_op_when_under_threshold() -> None:
    """Focused cancelling card with <5s → ``x`` no-ops."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        card = _make_card_with_state(state="cancelling", seconds_in_state=1.0)
        stub = _StubWorkflowsImpl()
        run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-000000000002")
        card.bind_workflow(workflows_impl=stub, run_id=run_id)

        await transcript.mount(card)
        await pilot.pause()

        card.action_force_kill()
        await pilot.pause()

        modals = list(app.query(ForceKillConfirm))
        assert modals == []


@pytest.mark.asyncio
async def test_force_kill_no_op_when_running() -> None:
    """Focused running card → ``x`` no-ops."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        card = _make_card_with_state(state="running", seconds_in_state=60.0)
        stub = _StubWorkflowsImpl()
        run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-000000000003")
        card.bind_workflow(workflows_impl=stub, run_id=run_id)

        await transcript.mount(card)
        await pilot.pause()

        card.action_force_kill()
        await pilot.pause()

        modals = list(app.query(ForceKillConfirm))
        assert modals == []


@pytest.mark.asyncio
async def test_force_kill_confirm_y_fires_cascade_cancel() -> None:
    """Pressing ``y`` on the modal calls ``cancel(run_id, cascade=True)``."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        stub = _StubWorkflowsImpl()
        run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-000000000004")

        modal = ForceKillConfirm(
            run_id=run_id,
            n_related=2,
            workflows_impl=stub,
        )
        await transcript.mount(modal)
        await pilot.pause()

        # Fire the confirm action directly (bindings tested via focus
        # tend to be flaky in Pilot; the action is the seam).
        await modal.action_confirm()
        await pilot.pause()

        assert stub.cancel_calls == [(run_id, True)]


@pytest.mark.asyncio
async def test_force_kill_confirm_n_unmounts_without_cancel() -> None:
    """Pressing ``n`` on the modal unmounts it and skips the cancel."""
    app = _HostApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        transcript = app.query_one("#transcript", VerticalScroll)

        stub = _StubWorkflowsImpl()
        run_id = WorkflowRunId("wrk_00000000-0000-0000-0000-000000000005")

        modal = ForceKillConfirm(
            run_id=run_id,
            n_related=1,
            workflows_impl=stub,
        )
        await transcript.mount(modal)
        await pilot.pause()

        # Fire the cancel action directly.
        await modal.action_cancel()
        await pilot.pause()

        assert stub.cancel_calls == []
        modals = list(app.query(ForceKillConfirm))
        assert modals == []
