"""``kolsch/nodes/noop`` runner — Phase 3.1 Step 2.

The smallest possible :class:`workflows.executor.NodeRunner` —
returns ``{}`` per master spec §4.1.4. Used for fan-out structural
placeholders and explicit single-entry-pattern shapes.
"""

from __future__ import annotations

from typing import Any

__all__ = ["NoopRunner"]


class NoopRunner:
    """The ``kolsch/nodes/noop`` runtime. Returns an empty dict."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,  # noqa: ANN401
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,  # noqa: ANN401
    ) -> dict[str, Any]:
        _ = (node_name, node_def, bound_inputs, state, ctx)
        return {}
