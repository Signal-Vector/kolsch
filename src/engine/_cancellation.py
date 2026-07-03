"""Cancellation helpers — race in-flight work against a CancelToken.

Two helpers for racing in-flight work against an
[`CancelToken`][engine.context.CancelToken]:
[`race_with_cancel`][engine._cancellation.race_with_cancel] (async)
and
[`wait_future_with_cancel`][engine._cancellation.wait_future_with_cancel]
(sync, for worker threads). Both helpers are no-ops when
`cancel_token` is None — programmatic test paths and engine-less
callers stay byte-identical.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import TYPE_CHECKING, Any, TypeVar

from errors import ExchangeCancelled

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from engine.context import CancelToken


T = TypeVar("T")


_DEFAULT_POLL_INTERVAL_SECONDS = 0.05
"""Worker-thread poll cadence — 50ms.

Gives the user-visible cancellation latency a soft cap of ~50ms in
the CrewAI worker-thread path while keeping busy-loop overhead
negligible (20 ticks/sec on idle wait). The async path doesn't poll
— it races directly on `cancel_token.wait_cancelled()`, which fires
immediately.
"""


_DEFAULT_POST_CANCEL_GRACE_SECONDS = 0.2
"""Grace window after cancellation flips, before in-flight work is preempted.

When cancellation fires DURING an in-flight tool call, the
in-progress work may be on the path to completion already (the chat
modal's `[n]` flow flips the cancel token AND returns a DENY effect
almost simultaneously — the observer's `tool.failed` emit completes
within a few event-loop ticks). Fast-finishing work lands its events
naturally rather than being truncated mid-emit. If the work hasn't
completed within the grace, the cancel preempts.

Picked to be larger than the typical observer emit latency (<10ms)
and smaller than the user-visible "did it hang?" threshold (~500ms).
Production calls that genuinely hang sit well above this and get
preempted.
"""


async def race_with_cancel[T](
    coro: Coroutine[Any, Any, T],
    cancel_token: CancelToken | None,
    *,
    what: str,
    post_cancel_grace_seconds: float = _DEFAULT_POST_CANCEL_GRACE_SECONDS,
) -> T:
    """Await `coro` racing against `cancel_token.wait_cancelled()`.

    When `cancel_token` is None, awaits `coro` directly with no
    overhead (the programmatic test path). Otherwise the coro and
    `wait_cancelled()` are scheduled as concurrent tasks and raced
    via `asyncio.wait(FIRST_COMPLETED)`. If the coro finishes first
    its result is returned; if the cancel fires first the coro gets
    `post_cancel_grace_seconds` to finish naturally (preserving fast
    modal-flip flows where `tool.failed` emits within a few ticks of
    the flip) before being preempted with
    [`ExchangeCancelled`][errors.ExchangeCancelled].

    Args:
        coro: The work coroutine to race.
        cancel_token: The exchange's cancellation handle; `None`
            short-circuits the race overhead.
        what: Short label used in the error path's `where=` field so
            JSONL readers can tell whether the cancellation hit an
            LLM call, a tool call, or a subprocess wait.
        post_cancel_grace_seconds: Grace window after cancel fires
            before the coro is preempted.

    Raises:
        ExchangeCancelled: The cancel fired and the coro did not
            finish within the grace window.
    """
    if cancel_token is None:
        # No token → no observer can flip it. Skip the race overhead
        # and just await the coro directly.
        return await coro

    # NOTE: don't short-circuit when `cancel_token.is_cancelled()`
    # at entry. The framework's tool-dispatch loop calls into us
    # for each tool call in a fan-out; if cancel flipped during an
    # earlier LLM call we still want THIS tool to have the grace
    # window to finish naturally (which preserves
    # tool.called → tool.result symmetry — see Scenario 3 in
    # tests/component/cli/test_chat_deny_stops_e2e.py). The grace
    # path below handles both pre-flip and during-flight cancel
    # uniformly: fast work lands, hung work preempts.
    work_task = asyncio.ensure_future(coro)
    cancel_task = asyncio.ensure_future(cancel_token.wait_cancelled())
    try:
        done, _pending = await asyncio.wait(
            {work_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        # Parent task was cancelled — propagate cleanly, abort both.
        work_task.cancel()
        cancel_task.cancel()
        raise

    if work_task in done:
        # The work completed first — drain the cancel waiter and
        # return the work's result (or re-raise its exception).
        cancel_task.cancel()
        try:
            await cancel_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return work_task.result()

    # Cancel fired first. Give the work a brief grace to complete
    # naturally — fast modal-flip flows finish within a few ticks
    # and we don't want to truncate their tool.failed emit.
    try:
        result = await asyncio.wait_for(work_task, timeout=post_cancel_grace_seconds)
    except TimeoutError:
        # Genuinely hung — preempt.
        work_task.cancel()
        try:
            await work_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        raise ExchangeCancelled(
            cancelled_at_event_id="",
            where=f"engine._cancellation.race_with_cancel[{what}]",
        ) from None
    else:
        return result


def wait_future_with_cancel[T](
    future: concurrent.futures.Future[T],
    cancel_token: CancelToken | None,
    *,
    what: str,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> T:
    """Block on `future` while polling `cancel_token`.

    Synchronous — designed for worker threads (CrewAI's executor)
    that can't `await` the async `wait_cancelled()`. Loops on
    `future.result(timeout=poll_interval)` and checks
    `cancel_token.is_cancelled()` between polls. On completion the
    future's result is returned (re-raising on exception, same as
    the unwrapped call); on cancel `future.cancel()` is called
    best-effort and
    [`ExchangeCancelled`][errors.ExchangeCancelled] is raised. When
    `cancel_token` is None, blocks indefinitely on
    `future.result()`.
    """
    if cancel_token is None:
        return future.result()

    if cancel_token.is_cancelled():
        future.cancel()
        raise ExchangeCancelled(
            cancelled_at_event_id="",
            where=f"engine._cancellation.wait_future_with_cancel[{what}]",
        )

    while True:
        try:
            return future.result(timeout=poll_interval)
        except concurrent.futures.TimeoutError:
            if cancel_token.is_cancelled():
                future.cancel()
                raise ExchangeCancelled(
                    cancelled_at_event_id="",
                    where=(
                        "engine._cancellation.wait_future_with_cancel"
                        f"[{what}]"
                    ),
                ) from None
            continue


def make_cancellation_callback(
    cancel_token: CancelToken | None,
) -> Callable[[], bool] | None:
    """Wrap a CancelToken in the poll-callback shape adapters expect.

    Returns `None` when the token is `None`. Centralised so the
    engine's `run_exchange` call site and the helpers above all spell
    this the same way.
    """
    if cancel_token is None:
        return None
    return cancel_token.is_cancelled
