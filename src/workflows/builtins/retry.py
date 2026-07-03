"""`with_retry` — per-invocation runner wrapper for `retry:` modifiers.

Wraps a [`NodeRunner`][workflows.executor.NodeRunner] so the inner
`run` is attempted `retries + 1` times. Intermediate failures are
swallowed — only the final attempt's success or failure surfaces
in the JSONL event stream. Between attempts, the wrapper sleeps
for a small backoff so a transient failure mode (network blip,
short-lived file lock) gets a real chance to clear.

The executor applies this wrapper per-invocation when the node's
`retry` modifier is non-zero; without the modifier, the raw runner
runs unwrapped. The v1 surface is a flat 100ms backoff — no
exponential growth, no jitter, no per-exception-class filter. The
`retry` field is a thin facade so future versions can extend it
via a dict shape without breaking the integer API. `tenacity` is
intentionally NOT introduced as a dep: a 6-line loop covers the
contract.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from workflows.executor import NodeRunner

__all__ = ["with_retry"]


_RETRY_BACKOFF_SECONDS = 0.1


def with_retry(
    runner: NodeRunner,
    *,
    retries: int,
    backoff_seconds: float = _RETRY_BACKOFF_SECONDS,
) -> NodeRunner:
    """Wrap `runner` so its `run` is attempted up to `retries + 1` times.

    Intermediate failures are swallowed (the wrapper sleeps
    `backoff_seconds` between attempts). The final attempt's
    exception is re-raised to the caller; the executor's per-node
    failure path emits `workflow.node.failed` for that single
    terminal exception.

    Args:
        runner: The inner runner whose `run` is repeated.
        retries: How many retries to perform after the initial
            attempt. `0` is a single attempt; `2` is three total.
        backoff_seconds: Flat delay between attempts.

    Returns:
        A runner with the same `NodeRunner` shape that delegates
        to `runner` with the retry loop applied.

    Raises:
        ValueError: `retries` is negative.
    """
    if retries < 0:
        msg = f"retries must be non-negative; got {retries}"
        raise ValueError(msg)

    total_attempts = retries + 1

    class _RetryWrapper:
        async def run(
            self,
            *,
            node_name: str,
            node_def: Any,
            bound_inputs: dict[str, Any],
            state: dict[str, Any],
            ctx: Any,
        ) -> dict[str, Any]:
            last_exc: BaseException | None = None
            for attempt in range(total_attempts):
                try:
                    return await runner.run(
                        node_name=node_name,
                        node_def=node_def,
                        bound_inputs=bound_inputs,
                        state=state,
                        ctx=ctx,
                    )
                except Exception as exc:  # noqa: BLE001 — retry blanket
                    last_exc = exc
                    if attempt + 1 < total_attempts:
                        await asyncio.sleep(backoff_seconds)
                        continue
            # Final attempt's exception propagates; ``last_exc`` is
            # always set when we exit the loop without returning
            # because the loop runs at least once.
            assert last_exc is not None  # noqa: S101 — invariant
            raise last_exc

    return _RetryWrapper()
