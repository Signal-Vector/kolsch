"""``kolsch/nodes/compute`` runner — Phase 3.6 §4.5.1.

General-purpose Jinja expression. Renders ``value:`` against the
edge-bound inputs overlay and returns ``{value: <any>}`` — the
typed return shape NativeEnvironment produces for single-expression
templates (a ``{{ 1 + 1 }}`` value is captured as ``int 2``, not
the string ``"2"``).

Subsumes the 8 aggregation node types (``sum`` / ``count`` / ``min``
/ ``max`` / ``sort`` / ``flatten`` / ``any`` / ``all``) per master
spec §4.0 lines 234-245 via stock Jinja filters (``| sum``,
``| length``, ``| min``, ``| max``, ``| sort``, ``| selectattr``,
``| rejectattr``). Step 1's :mod:`tests.component.test_workflow_compute_recipes`
ships the 8-case coverage that locks the typed-return contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflows.executor import _is_jinja_marked

if TYPE_CHECKING:
    from workflows.context import NodeContext
    from workflows.parser import ComputeNodeDef


__all__ = ["ComputeRunner"]


class ComputeRunner:
    """Runtime for ``kolsch/nodes/compute`` — Phase 3.6 §4.5.1."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: ComputeNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = (node_name, state)
        raw = node_def.value
        if _is_jinja_marked(raw):
            rendered = ctx.render_jinja(
                raw, inputs_overlay=bound_inputs
            )
        else:
            rendered = raw
        return {"value": rendered}
