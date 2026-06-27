"""``kolsch/nodes/filter`` runner — Phase 3.6 §4.4.1.

Per-element predicate-based list reducer. Renders ``over:`` to a
list, iterates with ``item_as`` shadowing same-named entries in
``bound_inputs`` per element, renders ``condition:`` against the
augmented scope, and returns ``{result: list[T]}`` of elements where
the predicate was truthy.

Non-list ``over:`` raises :class:`TypeError`; the executor's
worklist catches and emits ``workflow.node.failed`` → run aborts.
Validator does not type-check Jinja results (they're ``Any`` at
parse time), so the runner is the gate.

``item_as`` defaults to ``"item"`` per the parser model; per-element
binding shadows any same-named key in ``bound_inputs`` (the overlay
order in :meth:`_render_predicate` puts ``item_as`` AFTER the
existing inputs so the element wins on collision).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflows.executor import _is_jinja_marked

if TYPE_CHECKING:
    from workflows.context import NodeContext
    from workflows.parser import FilterNodeDef


__all__ = ["FilterRunner"]


class FilterRunner:
    """Runtime for ``kolsch/nodes/filter`` — Phase 3.6 §4.4.1."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: FilterNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
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
        """Jinja-or-literal — same Round 6 J5/β convention as the
        other Phase 3.5/3.6 runners. Non-string values pass through
        verbatim (e.g. an ``over:`` YAML list literal)."""
        if _is_jinja_marked(value):
            return ctx.render_jinja(value, inputs_overlay=overlay)
        return value
