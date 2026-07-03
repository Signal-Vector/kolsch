"""`with_timeout` — per-invocation runner wrapper for `timeout_seconds:`.

Wraps a [`NodeRunner`][workflows.executor.NodeRunner] so its `run`
invocation is bounded by an `asyncio.wait_for` deadline. On
expiry, raises
[`NodeTimeoutExceeded`][workflows.errors.NodeTimeoutExceeded]
carrying the offending node + invocation id + limit.

The executor applies this wrapper per-invocation when the node's
`timeout_seconds` modifier is set; without the modifier, the raw
runner runs unwrapped. Per-node timeout is independent of the
workflow-level `Workflow.timeout_seconds`, which the executor
enforces at the run boundary.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from workflows.errors import NodeTimeoutExceeded

if TYPE_CHECKING:
    from workflows.executor import NodeRunner

__all__ = ["with_timeout"]


def with_timeout(
    runner: NodeRunner,
    *,
    timeout_seconds: float,
) -> NodeRunner:
    """Wrap `runner` so its `run` is bounded by `asyncio.wait_for`.

    The wrapped runner reads `node_name` + `ctx.invocation_id` from
    the call site so the
    [`NodeTimeoutExceeded`][workflows.errors.NodeTimeoutExceeded]
    payload points at the right node + invocation.

    Args:
        runner: The inner runner whose `run` is timed.
        timeout_seconds: The deadline, in seconds.

    Returns:
        A runner with the same `NodeRunner` shape that delegates
        to `runner` with the deadline applied.
    """

    class _TimeoutWrapper:
        async def run(
            self,
            *,
            node_name: str,
            node_def: Any,
            bound_inputs: dict[str, Any],
            state: dict[str, Any],
            ctx: Any,
        ) -> dict[str, Any]:
            try:
                return await asyncio.wait_for(
                    runner.run(
                        node_name=node_name,
                        node_def=node_def,
                        bound_inputs=bound_inputs,
                        state=state,
                        ctx=ctx,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise NodeTimeoutExceeded(
                    node=node_name,
                    invocation_id=str(ctx.invocation_id),
                    limit=float(timeout_seconds),
                ) from exc

    return _TimeoutWrapper()
