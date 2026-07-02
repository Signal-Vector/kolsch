"""Unit tests for :mod:`engine._cancellation`.

Coverage for the helpers added by
``bug-framework-cancellation-mid-call-latency.md`` +
``bug-mid-tool-cancellation-latency.md``:

- :func:`race_with_cancel` — the async race used by the
  MinimalFramework's mid-LLM / mid-tool awaits and the bash tool's
  subprocess wait. Scenarios:
    - cancel_token is None → passthrough (Phase 1 backwards compat)
    - work completes first → returns the result, cancels the waiter
    - cancel fires during a FAST-completing work → grace window
      lets the work finish naturally (preserves modal-deny flow)
    - cancel fires during a HUNG work → grace window expires,
      preempts, raises ExchangeCancelled
    - pre-flipped cancel + fast work → work completes within grace,
      result returns (Scenario 3 of test_chat_deny_stops_e2e)
    - pre-flipped cancel + hung work → preempts immediately after
      grace

- :func:`wait_future_with_cancel` — the sync race used by CrewAI
  worker threads. Scenarios:
    - cancel_token is None → passthrough (blocks on future.result)
    - future completes first → returns the result
    - cancel fires while future hangs → cancels the future, raises
      ExchangeCancelled
    - pre-flipped cancel + future already done → returns result
      (no preemption needed)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from typing import Any

import pytest

from engine._cancellation import (
    race_with_cancel,
    wait_future_with_cancel,
)
from engine.context import CancelToken
from errors import ExchangeCancelled


# ---------------------------------------------------------------------------
# race_with_cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_race_with_cancel_passthrough_when_token_none() -> None:
    """No token → no race overhead, direct await."""

    async def _work() -> str:
        return "done"

    assert await race_with_cancel(_work(), None, what="probe") == "done"


@pytest.mark.asyncio
async def test_race_with_cancel_work_wins() -> None:
    """Work completes before cancel fires → returns work result."""
    token = CancelToken()

    async def _work() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    result = await race_with_cancel(_work(), token, what="probe")
    assert result == "ok"
    assert token.is_cancelled() is False


@pytest.mark.asyncio
async def test_race_with_cancel_fast_work_after_cancel_completes_within_grace() -> (
    None
):
    """Cancel fires THEN work completes within the grace window →
    the work's result is returned (preserves modal-deny flow where
    the tool flips cancel as part of returning its DENY error)."""
    token = CancelToken()

    async def _work() -> str:
        # Sleep briefly, flip cancel, then return — mirrors the modal
        # flow where the deny path flips cancel and emits tool.failed
        # within a few event-loop ticks.
        await asyncio.sleep(0.01)
        token.cancel()
        return "ok"

    result = await race_with_cancel(_work(), token, what="modal_like")
    assert result == "ok"
    assert token.is_cancelled() is True


@pytest.mark.asyncio
async def test_race_with_cancel_hung_work_preempted_after_grace() -> None:
    """Cancel fires while work is hanging → after the grace window
    the work is preempted and ExchangeCancelled raised."""
    token = CancelToken()

    async def _work() -> str:
        await asyncio.sleep(30)  # would hang in the test
        return "never"

    # Flip the cancel before the call — pre-flipped behavior.
    token.cancel()
    start = time.monotonic()
    with pytest.raises(ExchangeCancelled):
        await race_with_cancel(
            _work(), token, what="hang", post_cancel_grace_seconds=0.1
        )
    elapsed = time.monotonic() - start
    # Grace 100ms + scheduling overhead; should complete well under
    # 1s even on a loaded CI.
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_race_with_cancel_pre_flipped_with_fast_work_returns_result() -> None:
    """Pre-flipped cancel + fast-completing work → grace window
    lets the fast work finish; result returned. This is the
    cycle-boundary case for the framework's tool-dispatch loop."""
    token = CancelToken()
    token.cancel()  # pre-flipped

    async def _work() -> str:
        await asyncio.sleep(0.01)
        return "ok"

    result = await race_with_cancel(
        _work(), token, what="fast_after_preflip"
    )
    assert result == "ok"


@pytest.mark.asyncio
async def test_race_with_cancel_propagates_work_exception() -> None:
    """When the work raises, its exception propagates (not silently
    swallowed by the race plumbing)."""
    token = CancelToken()

    async def _work() -> str:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await race_with_cancel(_work(), token, what="probe")


# ---------------------------------------------------------------------------
# wait_future_with_cancel
# ---------------------------------------------------------------------------


def test_wait_future_with_cancel_passthrough_when_token_none() -> None:
    """No token → blocks on future.result indefinitely (the pre-QoL
    behaviour). Test with an already-resolved future to avoid
    blocking the test runner."""
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    future.set_result("ok")
    assert wait_future_with_cancel(future, None, what="probe") == "ok"


def test_wait_future_with_cancel_future_wins() -> None:
    """Future resolves quickly → returns its result."""
    token = CancelToken()
    future: concurrent.futures.Future[str] = concurrent.futures.Future()

    def _resolve_after_delay() -> None:
        time.sleep(0.02)
        future.set_result("ok")

    threading.Thread(target=_resolve_after_delay, daemon=True).start()
    result = wait_future_with_cancel(future, token, what="probe")
    assert result == "ok"


def test_wait_future_with_cancel_pre_flipped_raises_even_if_done() -> None:
    """Pre-flipped cancel: the worker thread sees the cancel state
    BEFORE looking at the future, so it raises ExchangeCancelled even
    when the future has already resolved successfully. This pins the
    worker-thread variant's contract: cancellation wins over a
    completed future. (The async variant in ``race_with_cancel``
    grants the work a grace window instead — see
    ``test_race_with_cancel_pre_flipped_with_fast_work_returns_result``
    for that side's behaviour.)"""
    token = CancelToken()
    token.cancel()
    future: concurrent.futures.Future[str] = concurrent.futures.Future()
    future.set_result("ok")
    with pytest.raises(ExchangeCancelled):
        wait_future_with_cancel(future, token, what="probe")


def test_wait_future_with_cancel_cancels_hung_future() -> None:
    """Future hangs and cancel flips → future.cancel() called and
    ExchangeCancelled raised. Verify the latency is bounded by the
    poll interval."""
    token = CancelToken()
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def _flip_cancel_after_delay() -> None:
        time.sleep(0.05)
        token.cancel()

    threading.Thread(target=_flip_cancel_after_delay, daemon=True).start()
    start = time.monotonic()
    with pytest.raises(ExchangeCancelled):
        wait_future_with_cancel(
            future, token, what="hung_future", poll_interval=0.01
        )
    elapsed = time.monotonic() - start
    # Should observe cancel within poll_interval + small slack.
    assert elapsed < 1.0
    # Future cancellation was attempted (may not succeed on a real
    # future running on a different loop, but the call was made).
    # The mock future here is not running anywhere so it just stays
    # pending — the test asserts the raise path executed.
