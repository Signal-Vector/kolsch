"""`kolsch/nodes/user_input` runner — pauses the run for operator input.

The smallest possible runner shape: render the node's `prompt:`
field against the executor's Jinja scope (so `{{ nodes.<X>.<Y> }}`
and `{{ inputs.<K> }}` resolve), then raise
[`WorkflowPausedForInput`][workflows.errors.WorkflowPausedForInput].

The runner NEVER returns. The pause signal is caught by the
executor's worklist (NOT the generic failure path), which writes
the `waiting_at` sidecar, flips run status to
`waiting_for_input`, and exits cleanly. The matching
`workflow.node.started` event for this invocation is already on
disk; the ABSENCE of a corresponding completion event IS the pause
marker. A separate resume entry point completes the open
invocation with the operator's response.
"""

from __future__ import annotations

from typing import Any

from workflows.errors import WorkflowPausedForInput

__all__ = ["UserInputRunner"]


class UserInputRunner:
    """Runner for `kolsch/nodes/user_input` — pauses for operator input.

    No constructor args — the runner depends only on the per-node
    context (the executor binds a Jinja renderer there at
    `NodeContext` construction time).
    """

    async def run(
        self,
        *,
        node_name: str,
        node_def: Any,
        bound_inputs: dict[str, Any],
        state: dict[str, Any],
        ctx: Any,
    ) -> dict[str, Any]:
        """Render `prompt:` and raise the pause signal — never returns.

        Raises:
            WorkflowPausedForInput: Always — carries the rendered
                prompt; caught by the executor's worklist.
        """
        _ = state
        prompt_template = getattr(node_def, "prompt", "")
        # Render the prompt through the executor's Jinja scope so
        # `{{ nodes.<X>.<Y> }}` and `{{ inputs.<K> }}` resolve
        # against the same captured + state + inputs scope the
        # executor uses for `binds:`. The edge-bound `bound_inputs`
        # overlays onto the workflow-level inputs.
        if isinstance(prompt_template, str) and "{{" in prompt_template:
            rendered_prompt = ctx.render_jinja(
                prompt_template, inputs_overlay=bound_inputs
            )
        else:
            rendered_prompt = prompt_template
        raise WorkflowPausedForInput(
            node_name=node_name,
            invocation_id=str(ctx.invocation_id),
            prompt=str(rendered_prompt),
        )
