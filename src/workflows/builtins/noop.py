"""`kolsch/nodes/noop` runner — the structural-placeholder runtime.

The smallest possible
[`NodeRunner`][workflows.executor.NodeRunner] — returns `{}`. Used
for fan-out structural placeholders and explicit
single-entry-pattern shapes.
"""

from __future__ import annotations

from typing import Any

__all__ = ["NoopRunner"]


class NoopRunner:
    """Runner for `kolsch/nodes/noop` — always captures an empty dict."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,  # noqa: ANN401
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,  # noqa: ANN401
    ) -> dict[str, Any]:
        """Return an empty dict; no side effects, no state read or write."""
        _ = (node_name, node_def, bound_inputs, state, ctx)
        return {}
