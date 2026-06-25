"""``with_timeout`` decorator — Phase 3.1 Step 2.

Wraps a :class:`workflows.executor.NodeRunner` so its
``run`` invocation is bounded by an :func:`asyncio.wait_for`
deadline. On expiry, raises :class:`NodeTimeoutExceeded` carrying
the offending node + invocation id + limit.

The executor applies this wrapper per-invocation when the node's
:attr:`timeout_seconds` modifier is set; without the modifier, the
raw runner runs unwrapped. Per-node timeout is independent of the
workflow-level :attr:`Workflow.timeout_seconds` (Step 5 owns that
deadline at the run boundary).
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
    """Return a runner that wraps ``runner.run`` in ``asyncio.wait_for``.

    The wrapped runner reads ``node_name`` + ``ctx.invocation_id``
    from the call site so the :class:`NodeTimeoutExceeded` payload
    points at the right node + invocation.
    """

    class _TimeoutWrapper:
        async def run(
            self,
            *,
            node_name: str,
            node_def: Any,  # noqa: ANN401
            bound_inputs: dict[str, Any],
            state: dict[str, Any],
            ctx: Any,  # noqa: ANN401
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
