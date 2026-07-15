"""bug-031 Test — `WorkflowRenderer` header elapsed ticks on wall-clock.

Session ``ses_af5980ab`` showed a 90.4s dead zone on the ``deduper``
node with the elapsed counter frozen at the last-seen event's
timestamp. Root cause: the pre-bug-031 ``WorkflowRenderer`` had no
periodic tick timer — the header only refreshed on inbound
``kolsch.workflow.progress`` UI events, so between executor events
the widget was static.

This test mounts a bare ``WorkflowRenderer``, feeds ``RUN_STARTED``
(so ``_start_time`` is set), waits 2 seconds wall-clock WITHOUT
sending any further events, and asserts the header's rendered
elapsed integer advanced. If the tick timer regresses to the
pre-bug-031 "event-driven only" shape, the render freezes at 0s and
this test fails loudly.

Wall-clock waits are unavoidable here — the failure mode is
"nothing happens between events" and the fix is "a wall-clock tick
timer drives it forward." A monotonic-clock mock would defeat the
regression signal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from cli.chat.widgets.card_body.workflow import WorkflowRenderer
from workflows.events import WorkflowEventKind

_WIRE = "kolsch.workflow.progress"


class _MockEvent:
    __slots__ = ("kind", "payload")

    def __init__(self, kind: str, payload: dict[str, Any]) -> None:
        self.kind = kind
        self.payload = payload


def _mk_run_started_payload(ts: datetime) -> dict[str, Any]:
    """Match the WIRE_WORKFLOW_PROGRESS payload shape for RUN_STARTED."""
    return {
        "run_id": "wrk_ticks",
        "workflow_ref": "kolsch/workflows/ticks",
        "kind": WorkflowEventKind.RUN_STARTED.value,
        "event_id": "ev_ticks_started",
        "ts": ts.isoformat(),
        "trace_id": "tr_ticks",
        "payload": {},
    }


@pytest.mark.asyncio
async def test_renderer_elapsed_advances_without_events(
    mock_app_factory,
) -> None:
    """The header's elapsed counter advances between executor events.

    Feeds RUN_STARTED at t0 anchored to ``datetime.now(UTC) - 1s`` so
    ``_elapsed_now`` (wall-clock diff from ``_start_time``) already
    reads at least 1s at first render. Waits ~2s wall-clock; asserts
    the rendered elapsed integer advanced by ≥ 1.
    """
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        # Anchor start-time slightly in the past so the very first
        # render doesn't read 0s (avoids a flaky "elapsed == 0"
        # baseline).
        started_at = datetime.now(tz=UTC)
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_run_started_payload(started_at),
            )
        )
        await pilot.pause()

        # Baseline: the elapsed integer from the header render.
        # ``_elapsed_seconds`` returns a float; the header
        # renders ``int(elapsed_seconds)``.
        baseline_elapsed = int(renderer._elapsed_now())

        # Wall-clock wait — no further ``apply_event`` calls. If the
        # tick timer is missing (pre-bug-031 shape) the header stays
        # at ``baseline_elapsed`` and ``_elapsed_now`` also stays put
        # (the pre-bug-031 code anchored on ``_last_ts or
        # datetime.now(UTC)`` — but ``_last_ts`` was set at t0 and
        # nothing advances it without an event).
        await pilot.pause(2.0)

        # Post-wait: the renderer's ``_elapsed_now`` should reflect the
        # wall-clock advance.
        after_elapsed = int(renderer._elapsed_now())
        assert after_elapsed >= baseline_elapsed + 1, (
            f"elapsed did not advance: baseline={baseline_elapsed}s "
            f"after={after_elapsed}s. Pre-bug-031 the header froze "
            f"on _last_ts and required an executor event to move — "
            f"the ``_tick_elapsed`` timer regressed."
        )

        # Sanity: the header's rendered string picks up the advance
        # from the 1Hz ``_tick_elapsed`` → ``_update_header`` path.
        # The last-observed integer may lag ``_elapsed_now`` by up to
        # 1s (the tick cadence), so assert the rendered value is at
        # least ``baseline_elapsed + 1`` — a strict "before advanced"
        # sanity that still would have failed pre-bug-031.
        header_render = str(renderer._header.render())
        header_seconds: int | None = None
        for candidate in range(after_elapsed, baseline_elapsed - 1, -1):
            if f"{candidate}s" in header_render:
                header_seconds = candidate
                break
        assert header_seconds is not None, (
            f"header render {header_render!r} carried no Ns value "
            f"in the range [{baseline_elapsed},{after_elapsed}] — "
            f"the 1Hz tick's call to ``_update_header`` never ran."
        )
        assert header_seconds > baseline_elapsed, (
            f"header render {header_render!r} shows Ns == baseline "
            f"({baseline_elapsed}s) even after 2s wall-clock — the "
            f"tick timer regressed."
        )


@pytest.mark.asyncio
async def test_renderer_stops_ticking_after_terminal(
    mock_app_factory,
) -> None:
    """Terminal events flip ``_is_terminal`` and the tick timer stops.

    Guards the "keep ticking forever after the run completes" regression
    — bug-031's ``_tick_elapsed`` early-outs on ``_is_terminal`` so the
    summary footer stays quiescent.
    """
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        started_at = datetime.now(tz=UTC)
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_run_started_payload(started_at),
            )
        )
        await pilot.pause()

        # Feed a RUN_COMPLETED terminal event.
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                {
                    "run_id": "wrk_ticks",
                    "workflow_ref": "kolsch/workflows/ticks",
                    "kind": WorkflowEventKind.RUN_COMPLETED.value,
                    "event_id": "ev_ticks_completed",
                    "ts": datetime.now(tz=UTC).isoformat(),
                    "trace_id": "tr_ticks",
                    "payload": {},
                },
            )
        )
        await pilot.pause()

        # ``_is_terminal`` flag set; tick handle stopped.
        assert renderer._is_terminal is True
        assert renderer._tick_handle is None


@pytest.mark.asyncio
async def test_renderer_stops_ticking_after_run_failed(
    mock_app_factory,
) -> None:
    """bug-063 VERIFY — ``run.failed`` also flips ``_is_terminal``.

    RUN_FAILED is in ``_TERMINAL_RUN_KINDS`` alongside RUN_COMPLETED
    (see ``card_body/workflow.py``). This test guards that the
    failure-side terminal path uses the same tick-stop machinery as
    the completion path. NO new production code.
    """
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        started_at = datetime.now(tz=UTC)
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_run_started_payload(started_at),
            )
        )
        await pilot.pause()

        # Feed a RUN_FAILED terminal event.
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                {
                    "run_id": "wrk_ticks",
                    "workflow_ref": "kolsch/workflows/ticks",
                    "kind": WorkflowEventKind.RUN_FAILED.value,
                    "event_id": "ev_ticks_failed",
                    "ts": datetime.now(tz=UTC).isoformat(),
                    "trace_id": "tr_ticks",
                    "payload": {},
                },
            )
        )
        await pilot.pause()

        assert renderer._is_terminal is True
        assert renderer._tick_handle is None


@pytest.mark.asyncio
async def test_node_failed_alone_does_not_freeze_card(
    mock_app_factory,
) -> None:
    """bug-063 GUARDRAIL — ``node.failed`` alone must NOT freeze the card.

    The card timer is RUN-level; freezing on a single node's failure
    would misreport a multi-node run (e.g. under future continue-on-
    failure semantics). This test prevents anyone from adding a
    tempting "freeze on node.failed" widget-side hack. If the
    renderer starts setting ``_is_terminal`` on ``NODE_FAILED``, this
    test fails loudly.

    Runs a NODE_STARTED → NODE_FAILED sequence WITHOUT a subsequent
    RUN_FAILED and asserts the card tick is still live.
    """
    renderer = WorkflowRenderer()
    app = mock_app_factory(renderer)
    async with app.run_test() as pilot:
        await pilot.pause()

        started_at = datetime.now(tz=UTC)
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                _mk_run_started_payload(started_at),
            )
        )
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                {
                    "run_id": "wrk_ticks",
                    "workflow_ref": "kolsch/workflows/ticks",
                    "kind": WorkflowEventKind.NODE_STARTED.value,
                    "event_id": "ev_node_started",
                    "ts": datetime.now(tz=UTC).isoformat(),
                    "trace_id": "tr_ticks",
                    "invocation_id": "inv_a",
                    "node_name": "a",
                    "payload": {},
                },
            )
        )
        renderer.apply_event(
            _MockEvent(
                _WIRE,
                {
                    "run_id": "wrk_ticks",
                    "workflow_ref": "kolsch/workflows/ticks",
                    "kind": WorkflowEventKind.NODE_FAILED.value,
                    "event_id": "ev_node_failed",
                    "ts": datetime.now(tz=UTC).isoformat(),
                    "trace_id": "tr_ticks",
                    "invocation_id": "inv_a",
                    "node_name": "a",
                    "payload": {"error_message": "boom"},
                },
            )
        )
        await pilot.pause()

        # Card tick is STILL live — node.failed alone must not freeze.
        assert renderer._is_terminal is False
        assert renderer._tick_handle is not None
