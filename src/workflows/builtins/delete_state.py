"""``kolsch/nodes/delete_state`` runner — Phase 3.5 Step 1.

Master spec §4.3.2. Iterates :attr:`DeleteStateNodeDef.keys` in
YAML declaration order. For each key:

- ``state.pop(key, None)`` — silent on absent keys. Matches Python
  ``dict.pop`` semantics; idempotency is intentional per master
  spec §4.3.2.
- Emit one :class:`WorkflowStateMutatedEvent` with
  ``op="delete"`` AND ``value=None`` regardless of whether the key
  was present. The event records the AUTHOR'S intent, not the
  observable mutation — replay applies a no-op ``pop`` consistently.

Returns ``{}`` — like ``set_state``, the observable effect is on
``_state``, not on the captured output dict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from workflows.events import (
    WorkflowStateMutatedEvent,
    WorkflowStateMutatedPayload,
)

if TYPE_CHECKING:
    from workflows.context import NodeContext
    from workflows.parser import DeleteStateNodeDef


__all__ = ["DeleteStateRunner"]


class DeleteStateRunner:
    """Runtime for ``kolsch/nodes/delete_state`` — Phase 3.5 §4.3.2."""

    async def run(
        self,
        *,
        node_name: str,
        node_def: DeleteStateNodeDef,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: NodeContext,
    ) -> dict[str, Any]:
        _ = (node_name, bound_inputs)
        for key in node_def.keys:
            state.pop(key, None)
            await ctx.emit_workflow_event(
                WorkflowStateMutatedEvent(
                    workflow_run_id=ctx.workflow_run_id,
                    trace_id=ctx.trace_id,
                    parent_event_id=ctx.parent_event_id,
                    payload=WorkflowStateMutatedPayload(
                        key=key,
                        op="delete",
                        value=None,
                    ),
                )
            )
        return {}
