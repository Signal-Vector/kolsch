"""``kolsch/nodes/set_state`` runner — Phase 3.5 Step 1.

Master spec §4.3.1. Iterates :attr:`SetStateNodeDef.keys` in YAML
declaration order. For each ``(key, value)`` pair:

- If the value is a Jinja-marked string (Round 6 J5/β — a ``str``
  containing ``"{{"``), render it through the executor's Jinja env
  via :meth:`NodeContext.render_jinja` with the receiver's edge-
  bound ``bound_inputs`` overlaying onto the workflow-level
  ``inputs.*`` namespace (Phase 3.3 §6.4.1).
- Otherwise (YAML literal — int, float, bool, list, dict, ``None``,
  or a plain string with no ``"{{"``), pass through verbatim.

Then mutate the executor's live ``_state`` dict and emit one
:class:`WorkflowStateMutatedEvent` per affected key. The emit's
``parent_event_id`` chains back to the node's
``workflow.node.started`` event (per spec §11.3) — the executor
already populates :attr:`NodeContext.parent_event_id` with that id.

Runner returns ``{}`` — state writes are observable through
``{{ state.* }}`` in downstream nodes, NOT through the captured
output dict (master spec §4.3.1).

**Multi-write evolution** (the canonical pattern per master spec
§4.3.1 lines 314-338) is supported by the live-dict design: a
subsequent ``set_state`` node referencing ``{{ state.attempts + 1 }}``
reads the LATEST mutation because the renderer sees the shared
``state`` dict, not a snapshot.

**Partial failure mid-iteration:** if Jinja rendering raises (e.g.
:class:`WorkflowJinjaRefUnknown` against a missing ``nodes.*``
reference), some keys have been mutated AND their events emitted;
the exception then propagates to the executor's failure path. The
JSONL retains the partial-mutation events for forensics — replay
applies them faithfully, matching the original (failed) run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflows.events import (
    WorkflowStateMutatedEvent,
    WorkflowStateMutatedPayload,
)
from workflows.executor import _is_jinja_marked

if TYPE_CHECKING:
    from workflows.context import NodeContext
    from workflows.parser import SetStateNodeDef


__all__ = ["SetStateRunner"]


class SetStateRunner:
    """Runtime for ``kolsch/nodes/set_state`` — Phase 3.5 §4.3.1."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: SetStateNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = node_name  # parent_event_id chain comes from ctx, not node_name
        for key, raw_value in node_def.keys.items():
            if _is_jinja_marked(raw_value):
                rendered = ctx.render_jinja(
                    raw_value, inputs_overlay=bound_inputs
                )
            else:
                rendered = raw_value
            state[key] = rendered
            await ctx.emit_workflow_event(
                WorkflowStateMutatedEvent(
                    workflow_run_id=ctx.workflow_run_id,
                    trace_id=ctx.trace_id,
                    parent_event_id=ctx.parent_event_id,
                    payload=WorkflowStateMutatedPayload(
                        key=key,
                        op="set",
                        value=rendered,
                    ),
                )
            )
        return {}
