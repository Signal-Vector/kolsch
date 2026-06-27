"""``kolsch/nodes/branch`` runner — Phase 3.6 §4.6.3.

Boolean route gate ORTHOGONAL to the edge-level ``branch:`` field
shipped in Phase 3.3. The Phase 3.3 mechanism reads
``compute_branch_value(node_def, captured)`` after every node
completion to decide which outgoing edges fire; this runner makes
``kolsch/nodes/branch`` a first-class branchable source by capturing
its ``condition:`` evaluation under the ``value`` key — the same
shape authored Python ``@node``s use for ``bool`` returns.

The condition follows Round 6 J5/β: a YAML literal (typically a bool,
sometimes an int via Jinja arithmetic upstream) or a Jinja-marked
string. ``bool(rendered)`` coerces both — defensive against
non-bool Jinja results (a truthy non-bool would otherwise be
rejected by :func:`workflows.branching.compute_branch_value`'s
``isinstance(value, bool)`` guard and route to ``None``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflows.executor import _is_jinja_marked

if TYPE_CHECKING:
    from workflows.context import NodeContext
    from workflows.parser import BranchNodeDef


__all__ = ["BranchRunner"]


class BranchRunner:
    """Runtime for ``kolsch/nodes/branch`` — Phase 3.6 §4.6.3."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: BranchNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = (node_name, state)
        raw = node_def.condition
        if _is_jinja_marked(raw):
            rendered = ctx.render_jinja(raw, inputs_overlay=bound_inputs)
        else:
            rendered = raw
        return {"value": bool(rendered)}
