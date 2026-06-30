"""`kolsch/nodes/branch` runner — first-class boolean route gate.

Orthogonal to the edge-level `branch:` field. The edge-level
mechanism reads
[`compute_branch_value`][workflows.branching.compute_branch_value]
after every node completion to decide which outgoing edges fire;
this runner makes `kolsch/nodes/branch` a first-class branchable
source by capturing its `condition:` evaluation under the `value`
key — the same shape authored Python `@node`s use for `bool`
returns.

The condition is a YAML literal (typically a bool, sometimes an
int via Jinja arithmetic upstream) or a Jinja-marked string;
`bool(rendered)` coerces both — defensive against non-bool Jinja
results that the branching helper's `isinstance(value, bool)` guard
would otherwise route to `None`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflows.executor import _is_jinja_marked

if TYPE_CHECKING:
    from workflows.context import NodeContext
    from workflows.parser import BranchNodeDef


__all__ = ["BranchRunner"]


class BranchRunner:
    """Runner for `kolsch/nodes/branch` — captures `{value: bool}`."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: BranchNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        """Render `condition:` and capture its truthiness under `value`."""
        _ = (node_name, state)
        raw = node_def.condition
        if _is_jinja_marked(raw):
            rendered = ctx.render_jinja(raw, inputs_overlay=bound_inputs)
        else:
            rendered = raw
        return {"value": bool(rendered)}
