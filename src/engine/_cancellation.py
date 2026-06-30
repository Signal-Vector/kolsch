"""Cancellation helpers — bug-framework-cancellation-mid-call-latency.md
+ bug-mid-tool-cancellation-latency.md.

Two helpers for racing in-flight work against an
:class:`engine.context.CancelToken`:

- :func:`race_with_cancel` — async-only. Wraps a coroutine in a task,
  awaits it racing against ``cancel_token.wait_cancelled()``. On
  cancel the task is cancelled and :class:`errors.ExchangeCancelled`
  raised. Used by the FakeFramework's mid-LLM / mid-tool awaits and
  by the bash tool's subprocess wait.

- :func:`wait_future_with_cancel` — sync. A worker thread's
  ``future.result()`` doesn't see an async cancel token; this
  function polls the future with a short timeout and checks
  ``cancel_token.is_cancelled()`` between polls. On cancel the
  future is cancelled and :class:`errors.ExchangeCancelled` raised
  (still in the worker thread; the CrewAI adapter's caller catches
  it). Used by the CrewAI worker threads in ``_CrewAILLM.call`` and
  ``_make_crewai_tool._run``.

Both helpers are no-ops when ``cancel_token`` is None — Phase 1
programmatic test paths and engine-less callers stay byte-identical.

Pre-QoL the FakeFramework only polled cancellation at iteration
boundaries (`fake_framework.py:77-79`), so a hung
``await llm(request)`` or ``await handle(...)`` would not see
cancellation until it returned on its own. The CrewAI adapter
polled once at the entry to ``_CrewAILLM.call`` then blocked on
``future.result()``. Both gaps close here.
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

50ms gives the user-visible cancellation latency a soft cap of
~50ms in the CrewAI worker-thread path while keeping the busy-loop
overhead negligible (20 ticks/sec on idle wait). The async path
(:func:`race_with_cancel`) doesn't poll — it races directly on
``cancel_token.wait_cancelled()``, which fires immediately.
"""


_DEFAULT_POST_CANCEL_GRACE_SECONDS = 0.2
"""Grace window after cancellation flips, before the in-flight work
is preempted.

When cancellation fires DURING an in-flight tool call, the
in-progress work may be on the path to completion already (the
chat modal's ``[n]`` flow flips the cancel token AND returns a
DENY effect almost simultaneously — the observer's
``tool.failed`` emit completes within a few event-loop ticks).
We want that fast-finishing work to land its events naturally
rather than being truncated mid-emit. The grace gives the work
``_DEFAULT_POST_CANCEL_GRACE_SECONDS`` to complete after cancel
fires; if it doesn't, the cancel preempts.

Picked to be larger than the typical observer emit latency
(<10ms) and smaller than the user-visible "did it hang?"
threshold (~500ms). Production calls that genuinely hang sit
well above this and get preempted.
"""


async def race_with_cancel(
    coro: Coroutine[Any, Any, T],
    cancel_token: CancelToken | None,
    *,
    what: str,
    post_cancel_grace_seconds: float = _DEFAULT_POST_CANCEL_GRACE_SECONDS,
) -> T:
    """Await ``coro`` racing against ``cancel_token.wait_cancelled()``.

    When ``cancel_token`` is None, awaits ``coro`` directly with no
    overhead (the Phase 1 programmatic test path). Otherwise:

    1. If the token is ALREADY flipped at entry, close the coro and
       raise :class:`errors.ExchangeCancelled` immediately.
    2. Schedule the coro and ``wait_cancelled()`` as concurrent tasks
       and ``asyncio.wait(FIRST_COMPLETED)`` on them.
    3. If the coro finishes first, return its result; cancel the
       waiter and drain it.
    4. If the cancel fires first, give the coro
       ``post_cancel_grace_seconds`` (default 200ms) to finish
       naturally. This preserves the chat modal's ``[n]`` flow where
       the in-flight tool call flips cancellation AND returns a
       DENY error within a few event-loop ticks: the work completes
       within the grace, its ``tool.failed`` event lands, and the
       cancel is observed at the framework's next iteration boundary
       (the existing pre-QoL behaviour).
    5. If the coro is still pending after the grace, cancel it and
       raise :class:`errors.ExchangeCancelled`. This is the
       genuinely-hung case the bug specs target.

    ``what`` is a short label used in the error path's ``where=``
    field so JSONL readers can tell whether the cancellation hit an
    LLM call, a tool call, or a subprocess wait.
    """
    if cancel_token is None:
        # No token → no observer can flip it. Skip the race overhead
        # and just await the coro directly (the Phase 1 path).
        return await coro

    # NOTE: don't short-circuit when ``cancel_token.is_cancelled()``
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
    except (asyncio.TimeoutError, TimeoutError):
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


def wait_future_with_cancel(
    future: concurrent.futures.Future[T],
    cancel_token: CancelToken | None,
    *,
    what: str,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> T:
    """Block on ``future`` while polling ``cancel_token``.

    Synchronous — designed for worker threads (CrewAI's executor)
    that can't ``await`` the async ``wait_cancelled()``. Loops on
    ``future.result(timeout=poll_interval)`` and checks
    ``cancel_token.is_cancelled()`` between polls:

    - Future completes → return its result (``.result()`` re-raises
      on exception, same as the unwrapped call).
    - Cancel flips → call ``future.cancel()`` (best effort; the
      main-loop coroutine may not abort instantly), then raise
      :class:`errors.ExchangeCancelled`.

    When ``cancel_token`` is None, blocks indefinitely on
    ``future.result()`` (the Phase 1 / pre-QoL behaviour).
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
    """Wrap a :class:`CancelToken` in the ``Callable[[], bool]`` shape
    framework adapters expect, or return None when the token is None.

    Centralised so the engine's ``run_exchange`` call site and the
    helpers above all spell this the same way.
    """
    if cancel_token is None:
        return None
    return cancel_token.is_cancelled
