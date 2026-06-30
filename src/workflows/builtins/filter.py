"""`kolsch/nodes/filter` runner — per-element predicate list reducer.

Renders `over:` to a list, iterates with `item_as` shadowing
same-named entries in `bound_inputs` per element, renders
`condition:` against the augmented scope, and returns
`{result: list[T]}` of elements where the predicate was truthy.

Non-list `over:` raises `TypeError`; the executor's worklist
catches and emits `workflow.node.failed` → run aborts. The
validator does not type-check Jinja results (they're `Any` at
parse time), so this runner is the gate.

`item_as` defaults to `"item"` per the parser model; per-element
binding shadows any same-named key in `bound_inputs` (the overlay
order puts `item_as` AFTER the existing inputs so the element wins
on collision).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflows.executor import _is_jinja_marked

if TYPE_CHECKING:
    from workflows.context import NodeContext
    from workflows.parser import FilterNodeDef


__all__ = ["FilterRunner"]


class FilterRunner:
    """Runner for `kolsch/nodes/filter` — per-element predicate reducer."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: FilterNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        """Filter `over:` by `condition:` per element and capture `result`.

        Raises:
            TypeError: `over:` did not resolve to a list.
        """
        _ = state
        rendered_over = self._render(
            node_def.over, ctx=ctx, overlay=bound_inputs
        )
        if not isinstance(rendered_over, list):
            msg = (
                f"node {node_name!r} filter.over must resolve to a "
                f"list; got {type(rendered_over).__name__}"
            )
            raise TypeError(msg)
        item_key = node_def.item_as
        result: list[Any] = []
        for element in rendered_over:
            element_overlay = {**bound_inputs, item_key: element}
            rendered_cond = self._render(
                node_def.condition, ctx=ctx, overlay=element_overlay
            )
            if bool(rendered_cond):
                result.append(element)
        return {"result": result}

    @staticmethod
    def _render(
        value: Any,
        *,
        ctx: NodeContext,
        overlay: dict[str, Any],
    ) -> Any:
        """Render `value` if Jinja-marked; pass non-strings through verbatim.

        Matches the convention shared with the other built-in
        runners. Non-string values (e.g. an `over:` YAML list
        literal) flow through unchanged.
        """
        if _is_jinja_marked(value):
            return ctx.render_jinja(value, inputs_overlay=overlay)
        return value
