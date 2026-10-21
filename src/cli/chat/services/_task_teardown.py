"""Shared cancel-and-wait for chat-service teardown (qol-143).

`aclose()` cancels a child task and then awaits it. The child's own
`CancelledError` is the expected outcome and is swallowed; a cancel
aimed at the task running `aclose()` must propagate instead. Wrapping
the await in `suppress(CancelledError)` ate both. This is the same
`Task.cancelling()` discrimination bug-128 / bug-140 use in
`WorkflowsImpl`, kept in one place for every teardown site.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def cancel_and_wait(
    task: asyncio.Task[Any],
    *,
    suppress_errors: bool = False,
) -> None:
    """Cancel `task` and wait for it to finish.

    Swallows the child's own `CancelledError`. Re-raises when the
    calling task is cancelled while parked here — detected as its
    `cancelling()` count rising above the value on entry, so a caller
    already unwinding a cancel (cleanup in `except CancelledError` /
    `finally`) still completes teardown rather than aborting at the
    first child. With `suppress_errors=True` the child's other
    `Exception`s are swallowed too (fire-and-forget tasks); otherwise
    they propagate.
    """
    current = asyncio.current_task()
    baseline = current.cancelling() if current is not None else 0
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if current is not None and current.cancelling() > baseline:
            raise
    except Exception:
        if not suppress_errors:
            raise
